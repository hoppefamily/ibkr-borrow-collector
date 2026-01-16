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

### Deploy Infrastructure

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

## Documentation

- 📖 **[DEPLOYMENT.md](DEPLOYMENT.md)** - Full deployment guide with CloudFormation
- 🔧 **[EXAMPLES.md](EXAMPLES.md)** - Data access patterns & code examples
- 🏗️ **[ARCHITECTURE.md](ARCHITECTURE.md)** - Delta compression strategy, failure modes, and system design

## Architecture Highlights

- **Delta compression**: 1.5 MB baseline + 26 KB deltas (98% savings)
- **Chained deltas**: Each snapshot references previous hour
- **Hourly baselines**: Prevent delta chain breakage
- **Change detection**: Skip uploads when data unchanged
- **Alpine Docker**: 50 MB image, <1s cold start
- **OIDC authentication**: No long-lived credentials

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
