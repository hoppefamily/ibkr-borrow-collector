# IBKR Borrow Rate Collector

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE-APACHE-2.0)

**Real-time borrow fee tracking for 17,000+ global securities**

Automated collection of Interactive Brokers' short-sale borrow rates and margin requirements across 18 global markets. Essential data infrastructure for flow trading analysis and short squeeze detection.

## Why This Exists

**Short-sale borrow fees are a critical signal for:**
- 🎯 **Short squeeze risk** - Sudden fee spikes indicate supply constraints
- 📊 **Market sentiment** - High fees reveal crowded shorts
- 💰 **Trading costs** - Direct impact on short position profitability
- 🔄 **Flow analysis** - Borrow availability affects order flow dynamics

Yet this data is scattered, delayed, and often paywalled. This collector makes it:
- ✅ **Free & open source**
- ✅ **Real-time** (15-minute updates)
- ✅ **Comprehensive** (18 markets: USA, UK, Germany, Switzerland, Italy, Japan, Hong Kong, Australia, Austria, Belgium, Canada, Netherlands, France, Mexico, Spain, Sweden, Singapore, India)
- ✅ **Efficient** (98% compression via delta encoding)

## Prerequisites

### System Requirements

**Required:**
- Python 3.11 or later
- `xdelta3` (for delta compression) - Install with:
  - **macOS**: `brew install xdelta`
  - **Ubuntu/Debian**: `apt-get install xdelta3`
  - **Alpine Linux**: `apk add xdelta3`
  - **Windows**: Download from [xdelta.org](http://xdelta.org/)

**Python Dependencies:**
```bash
pip install boto3>=1.26.0
```

**AWS Requirements:**
- AWS account with permissions to create:
  - S3 buckets
  - IAM roles and policies
  - OIDC providers
- AWS CLI configured (`aws configure`)

**GitHub Requirements (for automated collection):**
- GitHub repository with Actions enabled
- Repository secrets access (Settings → Secrets → Actions)

### Quick Verification

```bash
# Check xdelta3 is installed
xdelta3 -V

# Check AWS CLI is configured
aws sts get-caller-identity

# Check Python and boto3
python3 -c "import boto3; print(f'boto3 {boto3.__version__}')"
```

## What You Get

**Borrow rate data (.txt files):**
- Rebate rates (what you earn holding cash collateral)
- Fee rates (what you pay to borrow)
- Available shares (supply constraints)
- ISIN, FIGI, currency metadata

**Margin requirement data (.dat files):**
- Long/short initial margin percentages
- Maintenance margin requirements
- Concentration margin adjustments
- Per-security capital requirements

**Storage cost:** ~$0.20/month for complete global coverage
**Data volume:** 18 borrow markets + 8 margin markets
**Frequency:** 96 snapshots/day, 98% compression

## Quick Start

### Option 1: Local Collection (No AWS Required)

**Perfect for:** Testing, local analysis, or air-gapped environments

```bash
# Clone repository
git clone https://github.com/hoppefamily/ibkr-borrow-collector.git
cd ibkr-borrow-collector

# Run one-time collection (stores data in ./data/)
docker compose --profile local up

# Or run directly with Python
pip install -r requirements.txt
python collector.py --dry-run --log-json
```

**What you get:**
- Data downloaded to `./data/` directory
- Same folder structure as S3: `./data/YYYY-MM-DD/market-TIMESTAMP.txt`
- Hourly baselines (`.txt.gz`) + 15-minute deltas (`.xdelta`)
- MD5 checksums for verification
- JSON logs in stdout

**Optional: Sync to S3 later**
```bash
# Upload local data to your S3 bucket
aws s3 sync ./data/ s3://your-bucket/ibkr/borrow/ \
  --exclude "*.part" \
  --exclude "*.tmp"
```

### Option 2: Automated AWS Collection

**Perfect for:** Production monitoring, continuous data collection

```bash
# Clone repository
git clone https://github.com/hoppefamily/ibkr-borrow-collector.git
cd ibkr-borrow-collector

# Deploy AWS infrastructure (S3 + IAM role)
aws cloudformation create-stack \
  --stack-name ibkr-borrow-collector \
  --template-body file://cloudformation-template.yaml \
  --parameters \
    ParameterKey=GitHubRepository,ParameterValue=YOUR_USERNAME/ibkr-borrow-collector \
  --capabilities CAPABILITY_NAMED_IAM

# Get role ARN for GitHub Actions
aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].Outputs[?OutputKey==`GitHubActionsRoleArn`].OutputValue' \
  --output text
```

### Configure GitHub Secrets

Add in **Settings → Secrets → Actions**:
- `AWS_ROLE_ARN`: Role ARN from CloudFormation output
- `AWS_REGION`: `us-east-1` (or your region)
- `S3_BUCKET`: Bucket name from CloudFormation output

### Start Collecting

Enable GitHub Actions. Data collection starts automatically every 15 minutes during market hours.

### Reconstruct Snapshots

**Retrieve any snapshot from S3** (handles both baselines and deltas automatically):

```bash
# Reconstruct a specific snapshot
python collector.py reconstruct \
  --s3-bucket your-bucket \
  --s3-key ibkr/borrow/2026-01-16/usa-20260116T093000Z.txt.gz \
  --output usa-data.txt

# Or reconstruct a delta (automatically fetches baseline)
python collector.py reconstruct \
  --s3-bucket your-bucket \
  --s3-key ibkr/borrow/2026-01-16/usa-20260116T094500Z.xdelta \
  --output usa-data.txt
```

**Features:**
- Automatic baseline detection for deltas
- MD5 verification of reconstructed files
- Local baseline caching (speeds up multiple reconstructions)
- Works with both borrow (.txt) and margin (.dat) files

## Use Cases

### 1. Short Squeeze Detection
Monitor sudden borrow fee spikes that precede squeezes:
```python
# Example: Find stocks with 10x fee increase
import pandas as pd
from datetime import datetime, timedelta

# Load last 7 days of data
df = pd.read_csv('s3://your-bucket/ibkr/borrow/usa-reconstructed.csv')
df['timestamp'] = pd.to_datetime(df['timestamp'])

# Compare current fees to 24 hours ago
current = df[df['timestamp'] > datetime.now() - timedelta(hours=1)]
previous = df[df['timestamp'] > datetime.now() - timedelta(hours=25)]
previous = previous[previous['timestamp'] < datetime.now() - timedelta(hours=24)]

alerts = current[current['fee_rate'] / previous['fee_rate'] > 10]
print(f"Squeeze candidates: {alerts['symbol'].tolist()}")
```

### 2. Cost Analysis
Calculate daily short carrying costs:
```python
# Daily cost to short $100k of a stock
position_size = 100_000
fee_rate = 8.75  # percent (e.g., GME)
daily_cost = (fee_rate / 100) * position_size / 365
print(f"Daily carrying cost: ${daily_cost:.2f}")  # ~$24/day
```

### 3. Supply Monitoring
Track borrow availability constraints:
```python
# Find hard-to-borrow stocks (< 50k shares available)
import boto3
import gzip

s3 = boto3.client('s3')
obj = s3.get_object(Bucket='your-bucket', Key='ibkr/borrow/2026-01-16/usa-latest.txt.gz')
with gzip.open(obj['Body'], 'rt') as f:
    df = pd.read_csv(f, sep='|', skiprows=1)
    htb = df[df['AVAILABLE'] < 50000]
    print(f"Hard-to-borrow stocks: {len(htb)}")
```

### 4. Historical Analysis
Analyze fee trends over time:
```python
# Track borrow fee history for specific symbols
symbols = ['GME', 'AMC', 'TSLA']
history = df[df['symbol'].isin(symbols)].pivot(
    index='timestamp',
    columns='symbol',
    values='fee_rate'
)
history.plot(title='Borrow Fee Trends', ylabel='Fee Rate (%)')
```

## Data Format

**Borrow rates (.txt files):**
```csv
#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|
GME|USD|GAMESTOP CORP-CLASS A|270986868|US36467W1099|0.25|8.75|50000|BBG000BB5BF6|
```

**Margin requirements (.dat files):**
```csv
#SYM|CUR|NAME|CON|ISIN|CUSIP|LongMaintenanceMargin|LongInitialMargin|ShortMargin|Exchange|ShortInitialMargin|...
GME|USD|GAMESTOP CORP-CLASS A|270986868|US36467W1099|36467W109|100|100|300|ISLAND|300|...
```

**Compressed deltas** (98% space savings):
```
s3://your-bucket/ibkr/borrow/
├── 2026-01-16/
│   ├── usa-20260116_093000.txt.gz                      # Borrow baseline
│   ├── usa-20260116_094500.xdelta                      # Borrow delta
│   ├── stockmargin_final_dtls.IBLLC-US-093000.dat.gz  # Margin baseline
│   ├── stockmargin_final_dtls.IBLLC-US-094500.xdelta  # Margin delta
│   └── ...
```

## Data Contract (v1.0)

### Schema Versioning

All snapshots include schema version in S3 metadata:
```json
{
  "schema-version": "1.0",
  "collection-time": "2026-01-16T09:30:00+00:00",
  "snapshot-type": "baseline|delta",
  "file-type": "borrow|margin"
}
```

**Breaking changes** (requiring version bump):
- Column removals or reordering
- Data type changes
- Delimiter changes
- Compression format changes

**Non-breaking changes** (same version):
- Adding new columns at end
- Adding new markets
- Metadata additions

### Borrow Rate Schema (v1.0)

**File format:** Pipe-delimited (|) with header row

| Column | Type | Unit | Description | Example |
|--------|------|------|-------------|---------|
| `SYM` | string | - | Trading symbol | `GME` |
| `CUR` | string | ISO 4217 | Currency code | `USD` |
| `NAME` | string | - | Security name | `GAMESTOP CORP-CLASS A` |
| `CON` | integer | - | Contract ID | `270986868` |
| `ISIN` | string | ISO 6166 | International security ID | `US36467W1099` |
| `REBATERATE` | decimal | % per annum | Cash collateral rebate | `0.25` |
| `FEERATE` | decimal | % per annum | Borrow cost | `8.75` |
| `AVAILABLE` | integer | shares | Available shares | `50000` |
| `FIGI` | string | OpenFIGI | Financial instrument ID | `BBG000BB5BF6` |

**Important:**
- Fee rates are **annualized percentages** (e.g., `8.75` = 8.75% per year)
- Daily cost = `FEERATE / 100 / 365 * position_value`
- Empty fields represented as empty string between delimiters: `||`
- Header row always present (starts with `#`)

### Margin Requirement Schema (v1.0)

**File format:** Pipe-delimited (|) with header row

| Column | Type | Unit | Description |
|--------|------|------|-------------|
| `SYM` | string | - | Trading symbol |
| `CUR` | string | ISO 4217 | Currency |
| `NAME` | string | - | Security name |
| `CON` | integer | - | Contract ID |
| `ISIN` | string | ISO 6166 | International security ID |
| `CUSIP` | string | - | CUSIP identifier |
| `LongMaintenanceMargin` | decimal | % | Long maintenance % |
| `LongInitialMargin` | decimal | % | Long initial % |
| `ShortMargin` | decimal | % | Short maintenance % |
| `ShortInitialMargin` | decimal | % | Short initial % |
| `Exchange` | string | - | Primary exchange |

**Margin percentage interpretation:**
- `100` = 100% margin required (no leverage)
- `50` = 50% margin required (2:1 leverage)
- `300` = 300% margin required (concentrated position)

### Time Format (UTC Only)

**All timestamps are UTC** (ISO 8601 with timezone):
- Metadata: `2026-01-16T09:30:00+00:00`
- Filenames: `usa-20260116T093000Z.txt.gz`
- Logs: RFC 3339 format with explicit `+00:00`

**Market hours handling:**
- Collection runs every 15 minutes continuously
- Data availability depends on IBKR updates (typically during market hours)
- Empty collections (no changes) are logged but not uploaded

### Metadata Fields

Every S3 object includes:

| Metadata Key | Type | Description |
|--------------|------|-------------|
| `schema-version` | string | Contract version (`1.0`) |
| `collection-time` | ISO 8601 | UTC collection timestamp |
| `snapshot-type` | enum | `baseline` or `delta` |
| `file-type` | enum | `borrow` or `margin` |
| `original-md5` | string | MD5 of decompressed content |
| `original-size` | integer | Bytes of decompressed content |
| `source-key` | string | S3 key of source (for deltas) |
| `source-md5` | string | MD5 of source (for deltas) |
| `source-type` | enum | Always `baseline` (for deltas) |

### Reconstruction Guarantees

**Deterministic reconstruction:**
```bash
# Delta + baseline = exact original file
xdelta3 -d -s baseline.txt.gz current.xdelta original.txt
md5sum original.txt == metadata['original-md5']  # Always true
```

**Delta chain properties:**
- All deltas reference hourly baselines only (no chained deltas)
- Maximum reconstruction: 1 baseline + 1 delta
- Baselines created: XX:00, XX:30 (every 30 minutes)
- If baseline missing, next collection creates new baseline

## Operational Behavior

### Failure Modes

**1. Failed collection run**
- **Impact:** One 15-minute snapshot missing
- **Recovery:** Next run continues normally (snapshots are independent)
- **Detection:** Check CloudWatch logs for GitHub Actions failures

**2. IBKR format change**
- **Impact:** Parser errors, upload failures
- **Recovery:** Collector falls back to baseline snapshots
- **Detection:** Sudden increase in baseline uploads, errors in logs
- **Mitigation:** Monitor column count changes

**3. Partial uploads**
- **Impact:** None (S3 uploads are atomic)
- **Recovery:** Failed uploads leave no partial objects
- **Detection:** Check `failed` status in JSON logs

**4. S3 storage full / permissions**
- **Impact:** New snapshots fail to upload
- **Recovery:** Fix IAM permissions or storage quota
- **Detection:** S3 upload errors in logs, collector continues to collect

**5. xdelta3 not available**
- **Impact:** Only baseline snapshots created (98% compression lost)
- **Recovery:** Install xdelta3, future collections use deltas
- **Detection:** Warning in logs: `⚠ xdelta3 not available`

**6. Baseline missing for delta**
- **Impact:** Cannot create delta
- **Recovery:** Automatically creates new baseline
- **Detection:** Info log: `No previous baseline found, creating baseline instead`

**7. GitHub Actions rate limits**
- **Impact:** Collections skipped during rate limit
- **Recovery:** Resumes when rate limit resets
- **Detection:** GitHub Actions log shows rate limit errors

### Data Completeness

**Full day reconstruction:**
```python
# Get all snapshots for a market/day
import boto3

s3 = boto3.client('s3')
paginator = s3.get_paginator('list_objects_v2')

snapshots = []
for page in paginator.paginate(Bucket='bucket', Prefix='ibkr/borrow/2026-01-16/usa-'):
    snapshots.extend(page.get('Contents', []))

print(f"Total snapshots: {len(snapshots)}")
print(f"Expected: ~96 (24 hours * 4 snapshots/hour)")
```

**Identifying baselines vs deltas:**
```python
baselines = [s for s in snapshots if s['Key'].endswith('.txt.gz')]
deltas = [s for s in snapshots if s['Key'].endswith('.xdelta')]

print(f"Baselines: {len(baselines)} (every 30 min)")
print(f"Deltas: {len(deltas)} (15-min intervals)")
```

## Documentation

- 📖 **[DEPLOYMENT.md](DEPLOYMENT.md)** - Full deployment guide with CloudFormation
- 🔧 **[EXAMPLES.md](EXAMPLES.md)** - Data access patterns & code examples
- 🏗️ **[ARCHITECTURE.md](ARCHITECTURE.md)** - Delta compression strategy, failure modes, and system design

## Architecture Highlights

- **Delta compression**: 1.5 MB baseline + 26 KB deltas (98% savings)
- **Baseline-only deltas**: All deltas reference baselines directly (no chaining)
- **30-minute baselines**: New baseline every 30 minutes prevents chain breakage
- **Change detection**: Skip uploads when data unchanged (MD5 verification)
- **Alpine Docker**: 50 MB image, <1s cold start
- **OIDC authentication**: No long-lived credentials
- **Atomic operations**: FTP downloads and S3 uploads are atomic
- **Timezone-aware**: All timestamps in UTC (ISO 8601)

## Flow Trading Signals

This data infrastructure enables canonical flow-based features for squeeze detection and order flow analysis:

### 1. Fee Spike Z-Score
**Signal:** Sudden borrow cost increases indicate supply shock
```python
# Calculate 30-day rolling z-score of fee rates
df['fee_zscore'] = (df['fee_rate'] - df['fee_rate'].rolling(30*96).mean()) / \
                   df['fee_rate'].rolling(30*96).std()

# Alert: z-score > 2σ = significant squeeze pressure
squeeze_alerts = df[df['fee_zscore'] > 2.0]
```
**Flow interpretation:**
- `z > 2σ`: Supply constraint, potential squeeze setup
- `z > 3σ`: Severe shortage, forced covering likely
- Rapid spike + high volume = classic squeeze pattern

### 2. Availability Collapse Rate
**Signal:** Rate of borrow availability decline predicts covering urgency
```python
# Calculate 1-hour rate of change in available shares
df['avail_pct_change'] = df.groupby('symbol')['AVAILABLE'].pct_change(periods=4)

# Alert: >50% availability drop in 1 hour
rapid_decline = df[df['avail_pct_change'] < -0.5]
```
**Flow interpretation:**
- Slow decay (< -10%/day): Normal attrition
- Rapid collapse (> -50%/hour): Forced covering cascade
- Coupled with fee spike: Imminent squeeze

### 3. Fee Persistence & Decay
**Signal:** Fee duration distinguishes transient vs structural pressure
```python
# Calculate half-life of fee spikes
df['fee_above_threshold'] = df['fee_rate'] > df['fee_rate'].quantile(0.9)
df['spike_duration'] = df.groupby('symbol')['fee_above_threshold'] \
                          .apply(lambda x: x.rolling(96).sum())  # 24-hour window
```
**Flow interpretation:**
- Transient spike (< 3 days): Event-driven, mean-reversion trade
- Persistent spike (> 7 days): Structural shortage, avoid shorts
- Decay rate: Fast decay = temporary, slow decay = sustained pressure

### 4. Cross-Market Correlation
**Signal:** Multi-market fee divergence reveals venue arbitrage
```python
# Compare same security across markets (e.g., GME in USA vs Germany)
usa_fees = df[df['market'] == 'usa']['fee_rate']
ger_fees = df[df['market'] == 'germany']['fee_rate']
spread = usa_fees - ger_fees

# Alert: >5% spread = arbitrage or venue-specific squeeze
```
**Flow interpretation:**
- High correlation: Global liquidity event
- Low correlation: Venue-specific supply shock
- Spread widening: Cross-market arbitrage opportunity

### 5. Margin-Fee Coupling
**Signal:** Margin increases + fee spikes = clearinghouse stress
```python
# Join borrow fees with margin requirements
fees = pd.read_csv('borrow_rates.csv')
margins = pd.read_csv('margin_requirements.csv')
combined = fees.merge(margins, on=['symbol', 'timestamp'])

# Alert: High margin (>100%) AND high fee (>5%) = extreme risk
stressed = combined[(combined['ShortInitialMargin'] > 100) &
                    (combined['fee_rate'] > 5.0)]
```
**Flow interpretation:**
- Normal: Low margin (~30%) + low fee (~1%)
- Elevated: High margin OR high fee (caution)
- Critical: High margin AND high fee (systemic risk)

### Integration with Flow-State Monitor

These features feed into the broader Flow-State Monitor ecosystem:
- **Regime detection**: Borrow stress → flow regime transition
- **Position sizing**: High fee environments → reduce leverage
- **Entry timing**: Fee decay → optimal short entry
- **Risk management**: Fee spike → hedge or exit shorts

See **[market-state-detector](https://github.com/hoppefamily/market-state-detector)** for implementation of flow regime classification using these signals.

## Related Projects

- 📊 **[market-flow-dashboard](https://github.com/hoppefamily/market-flow-dashboard)** - Real-time visualization
- 🎯 **[market-state-detector](https://github.com/hoppefamily/market-state-detector)** - Regime detection
- 📈 **[trading](https://github.com/hoppefamily/trading)** - Trading strategy framework

## Contributing

Contributions welcome! Areas for improvement:
- Additional data sources (Fintel, S3 Partners, etc.)
- Query optimization for time-series analysis
- Integration with backtesting frameworks
- Data quality monitoring & alerts

## License

Apache License 2.0 - Free for commercial and personal use.

## Support

- 🐛 **Issues**: [GitHub Issues](https://github.com/hoppefamily/ibkr-borrow-collector/issues)
- 💬 **Discussions**: [GitHub Discussions](https://github.com/hoppefamily/ibkr-borrow-collector/discussions)

---

**Markets:** 18 borrow + 8 margin | **Cost:** ~$0.20/month | **Frequency:** Every 15 minutes
