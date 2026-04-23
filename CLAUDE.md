# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run feature engineering pipeline (reads S3, writes features.parquet)
python feature_engineering.py

# Run anomaly detection (reads features.parquet, writes anomalies.parquet + model artefacts)
python anomaly_model.py

# Set up Glue catalog + Athena view (idempotent, run once per environment)
python step1_setup_glue_athena.py
```

AWS credentials are resolved via `AWS_PROFILE` in `.env`. The `.env` file must be present; the script raises `ValueError` immediately if `AWS_PROFILE` is missing.

## Pipeline Architecture

Data flows through four stages:

```
lambda_function.py          lambda_producer.py
(Kinesis producer)    →     (Kinesis → S3)         →   S3: raw/YYYY/MM/DD/*.json
  ↓ env: STREAM_NAME          ↓ env: BUCKET_NAME

step1_setup_glue_athena.py                          feature_engineering.py
Glue crawler → trade_surv.raw_trades       →        S3: features/features.parquet
Athena view  → trade_surv.raw_trades_v                         ↓
                                                    anomaly_model.py
                                                    S3: processed/anomalies.parquet
                                                    S3: model/isolation_forest.pkl
                                                    S3: model/medians.json
```

**`lambda_function.py`** — Generates synthetic trades for 7 symbols (AAPL, MSFT, TSLA, AMZN, NVDA, GOOGL, META) with price random-walks, heavy-tailed volumes, and configurable off-hours/OTC ratios. Publishes N_TRADES records per invocation to the Kinesis stream named by `STREAM_NAME`. Key env vars: `STREAM_NAME`, `NUM_TRADES` (default 5), `EXT_HOURS_PCT` (default 0.10), `OTC_PCT` (default 0.15).

**`lambda_producer.py`** — Kinesis-triggered; decodes base64 records and writes newline-delimited JSON to S3 at `raw/{YYYY}/{MM}/{DD}/{HHMMSS-microseconds}.json`. Env vars: `BUCKET_NAME`, `RAW_PREFIX` (default `raw/`).

**`step1_setup_glue_athena.py`** — Idempotent setup script. Creates Glue database `trade_surv`, runs crawler `raw-trades-crawler` against `s3://trade-surveillance-bucket/raw/`, copies the crawler-managed `raw` table to the canonical `raw_trades` table, creates Athena workgroup `trade-surveillance-wg` (results → `s3://trade-surveillance-bucket/athena-results/`), and creates view `trade_surv.raw_trades_v`. Requires IAM role `AWSGlueServiceRole-trade-surv` to exist before running (creation commands are in the script's docstring).

**`feature_engineering.py`** — Downloads all 30,424+ NDJSON files from `raw/` using 32 parallel threads, engineers 12 features, and writes `features/features.parquet` (Snappy-compressed) using a write-to-temp-key + `copy_object` pattern for safe overwrites.

**`anomaly_model.py`** — Reads `features/features.parquet`, injects 50 labelled synthetic anomalies for recall validation, trains `IsolationForest(n_estimators=200, contamination=0.08, random_state=42)` on the full frame, scores every trade, runs SHAP TreeExplainer on flagged trades only, classifies anomaly type by rule, validates synthetic recall (target ≥ 80%), removes synthetics, and writes three artefacts to S3. Achieved: 88% synthetic recall, ~12,100 anomalies flagged (8% of 152,010 trades).

## AWS Resources

| Resource | Name |
|---|---|
| S3 bucket | `trade-surveillance-bucket` |
| Region | `us-east-2` |
| Glue database | `trade_surv` |
| Glue crawler | `raw-trades-crawler` |
| Glue table (canonical) | `trade_surv.raw_trades` |
| Glue table (crawler-managed) | `trade_surv.raw` |
| Athena view | `trade_surv.raw_trades_v` |
| Athena workgroup | `trade-surveillance-wg` |
| IAM account ID | `052893174124` |

## S3 Artefact Map

| S3 key | Written by | Content |
|---|---|---|
| `raw/YYYY/MM/DD/*.json` | `lambda_producer.py` | NDJSON trade records |
| `features/features.parquet` | `feature_engineering.py` | 152,010 rows × 32 cols, Snappy |
| `processed/anomalies.parquet` | `anomaly_model.py` | 152,010 rows × 40 cols, Snappy |
| `model/isolation_forest.pkl` | `anomaly_model.py` | Trained IsolationForest (~2.4 MB) |
| `model/medians.json` | `anomaly_model.py` | Per-feature medians for inference |
| `athena-results/` | Athena | Query result CSVs |

## Raw Data Schema

18 fields per record. Key details:
- `timestamp`: ISO-8601 with UTC offset (`+00:00`) — use `from_iso8601_timestamp()` in Athena, `pd.to_datetime(..., utc=True)` in pandas
- `side`: always `"Buy"` or `"Sell"` from the generator
- `exchange`: `NASDAQ`, `NYSE`, `BATS`, `ARCA`, `DARK`, `ATS01`, or `OTC`
- S3 partitioning is non-Hive (`YYYY/MM/DD/`) — Glue names the partition columns `partition_0`, `partition_1`, `partition_2`

## Feature Engineering Details

12 features added by `feature_engineering.py` (all float64 unless noted):

| Feature | Grouping |
|---|---|
| `spread`, `mid_price`, `relative_spread`, `depth_imbalance` | row-level |
| `z_score_price`, `z_score_volume` | symbol × date, ddof=1 |
| `trader_volume_share` | denominator = symbol × date total volume |
| `is_off_hours` (bool) | US/Eastern timezone, NYSE hours 09:30–16:00 |
| `is_otc` (bool) | row-level |
| `inter_arrival_time`, `return_vs_prev` | symbol, sorted by timestamp |
| `trader_buy_sell_ratio` | trader_id × date; sell-only traders → 0.0 |

Output schema: 18 original columns + `date` + 12 features = 32 columns.

## Anomaly Model Details

`anomaly_model.py` adds 8 columns to produce the 40-column `anomalies.parquet`:

| Column | dtype | Notes |
|---|---|---|
| `anomaly_score` | float64 | Raw `decision_function` output; **negative = more anomalous** |
| `is_anomaly` | bool | True if model flagged the row (8% of trades) |
| `anomaly_rank` | float64 | Rank 1 = most anomalous, 152,010 = most normal; re-ranked on real trades only |
| `top_3_shap_features` | str / None | JSON list of top-3 `[feature, shap_value]` pairs; flagged rows only |
| `top_shap_feature` | str / None | Strongest SHAP feature name; flagged rows only |
| `anomaly_type` | str / None | Rule-based: `fat_finger`, `volume_spike`, `off_hours`, `spoofing`, `wash_trade`, `multi_flag`, `unknown` |
| `injected` | bool | Always False in output (synthetics removed before save) |
| `injected_type` | str / None | Always None in output |

Anomaly type rules (applied to flagged trades only):

| Type | Condition |
|---|---|
| `fat_finger` | `z_score_price > 4` |
| `volume_spike` | `z_score_volume > 4` |
| `off_hours` | `is_off_hours == True` |
| `spoofing` | `abs(depth_imbalance) > 0.8` |
| `wash_trade` | `trader_buy_sell_ratio > 0.9` AND `z_score_volume > 2` |
| `multi_flag` | more than one condition matches |
| `unknown` | flagged but no condition matches |

To score new trades against the saved model: load `model/medians.json` for NaN-filling, `model/isolation_forest.pkl` for inference. Apply the same bool→int cast for `is_off_hours`/`is_otc` before calling `decision_function`.

## Athena Query Notes

- Always filter on `dt` to prune partitions: `WHERE dt = DATE '2024-01-15'`
- `timestamp` is a reserved word in Athena/Presto — always double-quote it: `"timestamp"`
- Numeric columns in the raw table may be inferred as strings by the Glue JSON classifier; the `raw_trades_v` view explicitly casts them to `DOUBLE`/`BIGINT`
