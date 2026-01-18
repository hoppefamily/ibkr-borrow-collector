# Parquet Cache

The Parquet cache layer converts daily CSV.gz snapshots into consolidated Parquet files for faster analytics.

## Benefits

- **5-10x better compression** than gzip
- **Columnar storage** for fast filtering and aggregation
- **Single file per day** instead of 96+ snapshots
- **Deduplication**: Only stores data points where values changed
- **Fast queries**: Parquet's columnar format is optimized for analytics

## Architecture

```
CSV.gz (96+ files/day) → Parquet (1 file/day) → Fast queries
```

## Usage

### Build cache for specific date

```bash
python build_parquet_cache.py --date 2026-01-18
```

### Build cache for date range

```bash
python build_parquet_cache.py --start 2026-01-01 --end 2026-01-31
```

### Rebuild existing caches

```bash
python build_parquet_cache.py --date 2026-01-18 --force
```

### List available dates

```bash
python build_parquet_cache.py --list-dates
```

Shows all dates with CSV.gz snapshots and whether they have Parquet cache (✓).

### Dry run

```bash
python build_parquet_cache.py --date 2026-01-18 --dry-run
```

Shows what would be done without actually building caches.

## Output Format

Parquet files are stored at:
```
s3://<bucket>/ibkr/parquet/<market>/<YYYY-MM-DD>.parquet
```

### Schema

```
symbol              : string
timestamp           : datetime64[ns, UTC]  (date only)
borrow_rate_annual  : float64              (percent per annum)
availability        : int64                (shares available)
snapshot_time       : datetime64[ns, UTC]  (exact time of snapshot)
```

### Example

```
symbol   timestamp            borrow_rate  availability  snapshot_time
AAPL     2026-01-18 00:00:00  0.25        10000000      2026-01-18 11:06:28
AAPL     2026-01-18 00:00:00  0.30        9500000       2026-01-18 15:21:15
TSLA     2026-01-18 00:00:00  2.50        500000        2026-01-18 11:06:28
```

## Deduplication

The cache only stores data points where `borrow_rate_annual` or `availability` changed compared to the previous snapshot for that symbol.

Typical compression:
- **~60-80% reduction** in row count
- Most symbols have stable rates throughout the day
- Only volatile symbols generate many rows

## Performance

### Query Speed Comparison

```python
# CSV.gz (slow): Download & parse 96 files
time: ~30-60 seconds for 1 day

# Parquet (fast): Download 1 file, columnar read
time: ~2-5 seconds for 1 day
```

### File Size Comparison

```
CSV.gz:    96 files × ~2 MB  = ~192 MB/day
Parquet:   1 file × ~15 MB   = ~15 MB/day  (92% reduction)
```

## Requirements

```bash
pip install -r requirements.txt
```

Dependencies:
- `boto3` - S3 access
- `pandas` - Data processing
- `pyarrow` - Parquet format

## Integration

### Python API

```python
import boto3
import pandas as pd
from datetime import date
from io import BytesIO

# Read Parquet cache
s3 = boto3.client('s3')
bucket = 'your-bucket'
key = f'ibkr/parquet/usa/2026-01-18.parquet'

response = s3.get_object(Bucket=bucket, Key=key)
df = pd.read_parquet(BytesIO(response['Body'].read()))

# Filter for specific symbols
aapl_data = df[df['symbol'] == 'AAPL']

# Analyze borrow rate changes
rate_changes = df.groupby('symbol').agg({
    'borrow_rate_annual': ['min', 'max', 'mean', 'std']
})
```

### AWS Athena

Parquet files can be queried directly with Athena:

```sql
CREATE EXTERNAL TABLE ibkr_borrow_parquet (
    symbol STRING,
    timestamp TIMESTAMP,
    borrow_rate_annual DOUBLE,
    availability BIGINT,
    snapshot_time TIMESTAMP
)
STORED AS PARQUET
LOCATION 's3://your-bucket/ibkr/parquet/usa/';

-- Query example
SELECT
    symbol,
    AVG(borrow_rate_annual) as avg_rate,
    MAX(borrow_rate_annual) as max_rate,
    COUNT(*) as changes
FROM ibkr_borrow_parquet
WHERE year = 2026 AND month = 1
GROUP BY symbol
HAVING avg_rate > 1.0
ORDER BY avg_rate DESC;
```

## Automation

### Manual Backfill

```bash
# Build cache for all available dates
python build_parquet_cache.py --list-dates > dates.txt
python build_parquet_cache.py --start 2026-01-01 --end 2026-12-31
```

### GitHub Actions Workflow (Future)

Could add a workflow that runs daily after collection:

```yaml
name: Build Parquet Cache
on:
  schedule:
    - cron: '30 2 * * *'  # After collection finishes
  workflow_dispatch:

jobs:
  build-cache:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python build_parquet_cache.py --date $(date -d yesterday +%Y-%m-%d)
        env:
          AWS_REGION: us-east-1
```

## Notes

- Parquet cache is **optional** - the original CSV.gz files remain the source of truth
- Cache can be rebuilt at any time with `--force`
- Uses Snappy compression (good balance of speed and size)
- PyArrow engine for best performance
- Timezone-aware: All timestamps in UTC
