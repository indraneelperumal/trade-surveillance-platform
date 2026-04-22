import collections
import io
import json
import os
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from uuid import uuid4

import boto3
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.config import Config
from dotenv import load_dotenv
from tqdm import tqdm

BUCKET = "trade-surveillance-bucket"
RAW_PREFIX = "raw/"
FEATURES_KEY = "features/features.parquet"

_bad_lines: collections.deque = collections.deque()  # thread-safe append


def load_env() -> str:
    load_dotenv()
    profile = os.environ.get("AWS_PROFILE")
    if not profile:
        raise ValueError(
            "AWS_PROFILE is not set. Add it to .env or export it in your shell."
        )
    return profile


def _make_s3_client(profile: str):
    session = boto3.Session(profile_name=profile)
    return session.client(
        "s3",
        config=Config(
            max_pool_connections=32,
            retries={"max_attempts": 5, "mode": "adaptive"},
        ),
    )


def list_s3_keys(s3_client, bucket: str, prefix: str) -> list:
    keys = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".json"):
                keys.append(key)
    if not keys:
        raise ValueError(f"No .json keys found under s3://{bucket}/{prefix}")
    return keys


def download_and_parse(s3_client, bucket: str, key: str) -> list:
    records = []
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        raw = response["Body"].read().decode("utf-8")
    except Exception as exc:
        warnings.warn(f"Failed to download {key}: {exc}")
        return records

    for line_no, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            _bad_lines.append((key, line_no, str(exc)))
            warnings.warn(f"Bad JSON in {key} line {line_no}: {exc}")
    return records


def load_raw_data(profile: str) -> pd.DataFrame:
    print("\n[1/4] Listing S3 keys ...")
    s3 = _make_s3_client(profile)
    keys = list_s3_keys(s3, BUCKET, RAW_PREFIX)
    print(f"      Found {len(keys):,} .json files")

    print(f"[2/4] Downloading & parsing ({len(keys):,} files, 32 workers) ...")
    all_records = []
    zero_record_files = 0

    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = {pool.submit(download_and_parse, s3, BUCKET, k): k for k in keys}
        for future in tqdm(as_completed(futures), total=len(futures), unit="file"):
            result = future.result()
            if result:
                all_records.extend(result)
            else:
                zero_record_files += 1

    if zero_record_files:
        warnings.warn(f"{zero_record_files} files returned zero valid records")
    if _bad_lines:
        warnings.warn(f"{len(_bad_lines)} total bad JSON lines skipped")

    print(f"      Parsed {len(all_records):,} records total")
    df = pd.DataFrame(all_records)

    if len(df) < 100_000:
        warnings.warn(
            f"Only {len(df):,} rows loaded — expected at least 100,000. "
            "Check for S3 access issues or data loss."
        )

    # Reduce memory and speed up groupby
    for col in ("symbol", "exchange", "side", "order_type"):
        if col in df.columns:
            df[col] = df[col].astype("category")

    return df


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    print("[3/4] Engineering features ...")

    # Normalize timestamp to UTC
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["date"] = df["timestamp"].dt.date

    # ── 1. spread ────────────────────────────────────────────────────────────
    df["spread"] = df["ask_price"] - df["bid_price"]

    # ── 2. mid_price ─────────────────────────────────────────────────────────
    df["mid_price"] = (df["bid_price"] + df["ask_price"]) / 2

    # ── 3. relative_spread ───────────────────────────────────────────────────
    df["relative_spread"] = df["spread"] / df["mid_price"]  # NaN if mid_price=0

    # ── 4. depth_imbalance ───────────────────────────────────────────────────
    size_sum = df["bid_size"] + df["ask_size"]
    df["depth_imbalance"] = (df["bid_size"] - df["ask_size"]) / size_sum  # NaN if 0

    # ── 5 & 6. z_score_price, z_score_volume (ddof=1) ────────────────────────
    grp = df.groupby(["symbol", "date"], observed=True)
    df["z_score_price"] = grp["price"].transform(
        lambda x: (x - x.mean()) / x.std(ddof=1)
    )
    df["z_score_volume"] = grp["volume"].transform(
        lambda x: (x - x.mean()) / x.std(ddof=1)
    )

    # ── 7. trader_volume_share ────────────────────────────────────────────────
    # Denominator: total volume per symbol per day (not per trader)
    total_vol = df.groupby(["symbol", "date"], observed=True)["volume"].transform("sum")
    df["trader_volume_share"] = df["volume"] / total_vol

    # ── 8. is_off_hours ───────────────────────────────────────────────────────
    # Convert to US/Eastern so pandas handles EST/EDT offsets automatically.
    # NYSE regular hours: 09:30–16:00 Eastern (handles both EST and EDT).
    eastern = df["timestamp"].dt.tz_convert("US/Eastern")
    hour = eastern.dt.hour
    minute = eastern.dt.minute
    df["is_off_hours"] = (hour < 9) | ((hour == 9) & (minute < 30)) | (hour >= 16)

    # ── 9. is_otc ─────────────────────────────────────────────────────────────
    df["is_otc"] = df["exchange"] == "OTC"

    # ── 10. inter_arrival_time ────────────────────────────────────────────────
    df = df.sort_values(["symbol", "timestamp"]).reset_index(drop=True)
    df["inter_arrival_time"] = (
        df.groupby("symbol", observed=True)["timestamp"]
        .diff()
        .dt.total_seconds()
    )

    # ── 11. return_vs_prev ────────────────────────────────────────────────────
    prev_price = df.groupby("symbol", observed=True)["price"].shift(1)
    df["return_vs_prev"] = np.where(
        (prev_price > 0) & (df["price"] > 0),
        np.log(df["price"] / prev_price),
        np.nan,
    )

    # ── 12. trader_buy_sell_ratio ─────────────────────────────────────────────
    known_sides = {"Buy", "Sell"}
    unexpected = df[~df["side"].isin(known_sides)]
    if len(unexpected) > 0:
        warnings.warn(
            f"{len(unexpected):,} rows have unexpected side values: "
            f"{unexpected['side'].unique().tolist()}"
        )

    valid = df[df["side"].isin(known_sides)].copy()
    valid["date_"] = valid["date"]  # avoid groupby conflict
    buy_counts = (
        valid[valid["side"] == "Buy"]
        .groupby(["trader_id", "date_"])
        .size()
        .rename("buy_count")
    )
    total_counts = (
        valid.groupby(["trader_id", "date_"])
        .size()
        .rename("total_count")
    )
    ratio = (
        buy_counts.reindex(total_counts.index).fillna(0) / total_counts
    ).rename("trader_buy_sell_ratio")
    ratio_df = ratio.reset_index().rename(columns={"date_": "date"})

    df = df.merge(ratio_df, on=["trader_id", "date"], how="left")

    print(f"      Features complete — DataFrame shape: {df.shape}")
    return df


def write_features(df: pd.DataFrame, profile: str) -> None:
    print("[4/4] Writing features.parquet to S3 ...")
    s3 = _make_s3_client(profile)

    # Serialize to in-memory Parquet buffer
    table = pa.Table.from_pandas(df)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    size_mb = buf.tell() / 1_048_576  # capture size before seek
    buf.seek(0)

    # Write to temp key first, then atomic copy to final key
    tmp_key = f"features/features_tmp_{uuid4()}.parquet"
    final_key = FEATURES_KEY

    try:
        s3.put_object(Bucket=BUCKET, Key=tmp_key, Body=buf)
        s3.copy_object(
            Bucket=BUCKET,
            CopySource={"Bucket": BUCKET, "Key": tmp_key},
            Key=final_key,
        )
        s3.delete_object(Bucket=BUCKET, Key=tmp_key)
    except Exception:
        try:
            s3.delete_object(Bucket=BUCKET, Key=tmp_key)
        except Exception:
            pass
        raise

    print(f"      Written: s3://{BUCKET}/{final_key}")
    print(f"      Rows: {len(df):,}  |  Size: {size_mb:.1f} MB  |  Compression: snappy")


def main():
    print("=" * 60)
    print("  feature_engineering.py")
    print("=" * 60)

    profile = load_env()
    print(f"  Source      : s3://{BUCKET}/{RAW_PREFIX}")
    print(f"  Output      : s3://{BUCKET}/{FEATURES_KEY}")

    df = load_raw_data(profile)
    df = engineer_features(df)
    write_features(df, profile)

    print("\nDone.")


if __name__ == "__main__":
    main()
