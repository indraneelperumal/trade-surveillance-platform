import functools
import io
import json
import warnings
from uuid import uuid4

import boto3
import pandas as pd
from botocore.config import Config

BUCKET        = "trade-surveillance-bucket"
ANOMALIES_KEY = "processed/anomalies.parquet"
FEATURES_KEY  = "features/features.parquet"
MEMOS_PREFIX  = "memos"


def _make_s3_client(profile: str):
    session = boto3.Session(profile_name=profile)
    return session.client(
        "s3",
        config=Config(
            max_pool_connections=10,
            retries={"max_attempts": 5, "mode": "adaptive"},
        ),
    )


def _s3_download_parquet(profile: str, key: str) -> pd.DataFrame:
    s3 = _make_s3_client(profile)
    buf = io.BytesIO()
    s3.download_fileobj(BUCKET, key, buf)
    buf.seek(0)
    return pd.read_parquet(buf)


def _s3_upload(s3_client, bucket: str, final_key: str, data: bytes) -> None:
    """Upload bytes via temp-key + copy_object for safe concurrent overwrites."""
    prefix, _, fname = final_key.rpartition("/")
    tmp_key = f"{prefix}/tmp_{uuid4()}_{fname}" if prefix else f"tmp_{uuid4()}_{fname}"
    try:
        s3_client.put_object(Bucket=bucket, Key=tmp_key, Body=data)
        s3_client.copy_object(
            Bucket=bucket,
            CopySource={"Bucket": bucket, "Key": tmp_key},
            Key=final_key,
        )
        s3_client.delete_object(Bucket=bucket, Key=tmp_key)
    except Exception:
        try:
            s3_client.delete_object(Bucket=bucket, Key=tmp_key)
        except Exception as cleanup_exc:
            warnings.warn(f"Failed to delete temp key {tmp_key}: {cleanup_exc}")
        raise


@functools.lru_cache(maxsize=None)
def _load_anomalies_df(profile: str) -> pd.DataFrame:
    """Download processed/anomalies.parquet once; cached by profile."""
    return _s3_download_parquet(profile, ANOMALIES_KEY)


@functools.lru_cache(maxsize=None)
def _load_features_df(profile: str) -> pd.DataFrame:
    """Download features/features.parquet once; cached by profile."""
    return _s3_download_parquet(profile, FEATURES_KEY)


def load_anomaly_record(trade_id: str, profile: str) -> dict:
    df = _load_anomalies_df(profile)
    matches = df[df["trade_id"] == trade_id]
    if matches.empty:
        raise ValueError(f"trade_id '{trade_id}' not found in anomalies.parquet")
    row = matches.iloc[0]
    if not row.get("is_anomaly", False):
        raise ValueError(f"trade_id '{trade_id}' exists but is not flagged as anomalous")
    return row.to_dict()


def load_trader_history(trader_id: str, profile: str, n: int = 30) -> pd.DataFrame:
    df = _load_features_df(profile)
    history = df[df["trader_id"] == trader_id].copy()
    if history.empty:
        return history
    history = history.sort_values("timestamp", ascending=False).head(n)
    return history.reset_index(drop=True)


def compute_trader_stats(history_df: pd.DataFrame) -> dict:
    if history_df.empty:
        return {}
    n = len(history_df)
    buy_count = (history_df["side"] == "Buy").sum() if "side" in history_df.columns else 0
    off_hours = history_df["is_off_hours"].sum() if "is_off_hours" in history_df.columns else 0
    otc_count = history_df["is_otc"].sum() if "is_otc" in history_df.columns else 0
    return {
        "trade_count": n,
        "avg_price": round(float(history_df["price"].mean()), 4) if "price" in history_df.columns else None,
        "avg_volume": round(float(history_df["volume"].mean()), 4) if "volume" in history_df.columns else None,
        "off_hours_rate": round(float(off_hours / n), 4),
        "otc_rate": round(float(otc_count / n), 4),
        "buy_sell_ratio": round(float(buy_count / n), 4),
        "avg_trader_volume_share": round(
            float(history_df["trader_volume_share"].mean()), 4
        ) if "trader_volume_share" in history_df.columns else None,
    }


def load_market_window(
    symbol: str,
    timestamp: pd.Timestamp,
    profile: str,
    window_minutes: int = 60,
) -> pd.DataFrame:
    df = _load_features_df(profile)
    sym_df = df[df["symbol"] == symbol].copy()
    if sym_df.empty:
        return sym_df
    ts = pd.Timestamp(timestamp)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    trade_ts_col = sym_df["timestamp"]
    if trade_ts_col.dt.tz is None:
        trade_ts_col = trade_ts_col.dt.tz_localize("UTC")
    delta = (trade_ts_col - ts).abs()
    window = sym_df[delta <= pd.Timedelta(minutes=window_minutes)]
    return window.reset_index(drop=True)


def compute_market_context(window_df: pd.DataFrame, trade: dict) -> dict:
    if window_df.empty:
        return {
            "symbol_trade_count_window": 0,
            "symbol_avg_volume_window": 0.0,
            "symbol_avg_price_window": 0.0,
            "symbol_volume_spike": False,
            "price_deviation_from_window_mean": 0.0,
        }
    avg_volume = float(window_df["volume"].mean()) if "volume" in window_df.columns else 0.0
    avg_price  = float(window_df["price"].mean())  if "price"  in window_df.columns else 0.0
    trade_vol  = float(trade.get("volume", 0) or 0)
    trade_price = float(trade.get("price", 0) or 0)
    volume_spike = trade_vol > 2 * avg_volume if avg_volume > 0 else False
    price_dev = (trade_price - avg_price) / avg_price if avg_price > 0 else 0.0
    return {
        "symbol_trade_count_window": len(window_df),
        "symbol_avg_volume_window": round(avg_volume, 4),
        "symbol_avg_price_window": round(avg_price, 4),
        "symbol_volume_spike": bool(volume_spike),
        "price_deviation_from_window_mean": round(price_dev, 6),
    }


def upload_memo_to_s3(trade_id: str, memo: dict, profile: str) -> None:
    s3 = _make_s3_client(profile)
    final_key = f"{MEMOS_PREFIX}/{trade_id}.json"
    data = json.dumps(memo, indent=2, default=str).encode("utf-8")
    _s3_upload(s3, BUCKET, final_key, data)
