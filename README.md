# IBKR Borrow Rate Collector

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE-APACHE-2.0)
[![License: GPL 3.0](https://img.shields.io/badge/License-GPL%203.0-blue.svg)](LICENSE-GPL-3.0)

Automated collector for Interactive Brokers short borrow rates with **98% delta compression**. Downloads real-time borrow availability and rates from IBKR's public FTP server for 17,000+ stocks across 7 global markets.

## Features

- **98% Storage Reduction**: xdelta3 binary diff compression (250 bytes/snapshot vs 600 KB baseline)
- **7 Global Markets**: USA, UK, Germany, Switzerland, Italy, Japan, Hong Kong
- **17,000+ Stocks**: Comprehensive coverage of IBKR shortable securities
- **15-Minute Frequency**: Near real-time data collection during market hours
- **Chained Deltas**: Efficient delta-from-delta compression with hourly baselines
- **Change Detection**: Only uploads when data actually changes
- **Containerized**: Alpine Linux Docker image (~50 MB) for fast startup
- **Flexible Deployment**: GitHub Actions, AWS Lambda, ECS, or local execution
- **Baseline Caching**: Local cache reduces S3 downloads by 90%
- **Free to Run**: ~$2/year total infrastructure cost on AWS

## Quick Start

### 1. Deploy Infrastructure (AWS CloudFormation)

```bash
# Deploy S3 bucket and IAM resources with OIDC authentication (recommended)
aws cloudformation create-stack \
  --stack-name ibkr-borrow-collector \
  --template-body file://cloudformation-template.yaml \
  --parameters \
    ParameterKey=UseOIDC,ParameterValue=true \
    ParameterKey=GitHubRepository,ParameterValue=YOUR_USERNAME/ibkr-borrow-collector \
  --capabilities CAPABILITY_NAMED_IAM

# Wait for completion
aws cloudformation wait stack-create-complete \
  --stack-name ibkr-borrow-collector

# Get role ARN and bucket name (for OIDC)
aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].Outputs[?OutputKey==`GitHubActionsRoleArn` || OutputKey==`BucketName`]'

# Configure GitHub Secrets (Settings → Secrets → Actions):
# AWS_ROLE_ARN: <from output above>
# AWS_REGION: us-east-1
# S3_BUCKET: <from output above>
```

✅ **No access keys needed!** GitHub Actions authenticates via OIDC.

See [DEPLOYMENT.md](DEPLOYMENT.md) for detailed deployment instructions and legacy access key method.

### 2. Docker (Recommended)

```bash
# Build the container
docker build -t ibkr-collector .

# Run collector (requires AWS credentials)
docker run --rm \
  -e AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY \
  -e AWS_DEFAULT_REGION=us-east-1 \
  ibkr-collector \
  --s3-bucket your-bucket-name \
  --s3-prefix ibkr/borrow
```

### Local Python

```bash
# Install dependencies
pip install -r requirements.txt

# Install xdelta3 (required for compression)
# macOS:
brew install xdelta

# Linux:
apt-get install xdelta3

# Run collector
python collector.py \
  --s3-bucket your-bucket-name \
  --s3-prefix ibkr/borrow
```

### Test FTP Connection

```bash
# Test IBKR FTP access (no AWS credentials needed)
python collector.py --test-connection --dry-run
```

## Data Format

### Storage Structure

```
s3://your-bucket/ibkr/borrow/
└── 2026-01-16/
    ├── usa-20260116_000530.txt.gz      # Hourly baseline (614 KB)
    ├── usa-20260116_001530.xdelta      # Delta (250 bytes)
    ├── usa-20260116_003030.xdelta      # Delta (250 bytes)
    ├── usa-20260116_010030.txt.gz      # Hourly baseline (614 KB)
    ├── british-20260116_000530.txt.gz
    ├── british-20260116_001530.xdelta
    └── ...
```

### File Types

**Baselines** (.txt.gz):
- Created at the start of each hour (00-09 minutes)
- Gzip-compressed raw FTP data
- ~600 KB for USA market
- Used for fast reconstruction and corruption recovery

**Deltas** (.xdelta):
- Created every 15 minutes between baselines
- Binary diff from previous snapshot
- ~250 bytes typical size (98.8% savings)
- Chain from previous delta or baseline

### Raw Data Format

IBKR FTP files are pipe-delimited with these fields:

```
#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE
AAPL|USD|APPLE INC|129430464|US0378331005|-0.25|0.6025|>10000000
TSLA|USD|TESLA INC|76792991|US88160R1014|-0.25|0.6581|>10000000
GME|USD|GAMESTOP CORP-CLASS A|13898641|US36467W1099|-0.25|0.5554|>10000000
```

Fields:
- **SYM**: Stock symbol
- **CUR**: Currency
- **NAME**: Company name
- **CON**: IBKR contract ID
- **ISIN**: International Securities ID
- **REBATERATE**: Rebate rate (usually negative)
- **FEERATE**: Annual borrow rate (% per year)
- **AVAILABLE**: Shares available (exact count or ">10000000")

## S3 Metadata

Each uploaded file includes metadata for reconstruction:

**Baselines**:
```json
{
  "original-md5": "abc123...",
  "file-type": "borrow",
  "snapshot-type": "baseline",
  "collection-time": "2026-01-16T12:00:00Z",
  "original-size": "1150000",
  "compressed-size": "614000"
}
```

**Deltas**:
```json
{
  "original-md5": "def456...",
  "source-md5": "abc123...",
  "source-key": "ibkr/borrow/2026-01-16/usa-20260116_115530.xdelta",
  "source-type": "delta",
  "file-type": "borrow",
  "snapshot-type": "delta",
  "collection-time": "2026-01-16T12:15:00Z",
  "original-size": "1150000",
  "delta-size": "250"
}
```

## Deployment Options

### CloudFormation (Recommended)

Use the included [cloudformation-template.yaml](cloudformation-template.yaml) to deploy complete infrastructure:

```bash
aws cloudformation create-stack \
  --stack-name ibkr-borrow-collector \
  --template-body file://cloudformation-template.yaml \
  --capabilities CAPABILITY_NAMED_IAM
```

**Includes**:
- S3 bucket with encryption, versioning, lifecycle policies
- IAM user and access keys for GitHub Actions
- IAM roles for Lambda and ECS deployments
- Intelligent-Tiering and Glacier transitions
- Secrets Manager for credential storage

See [DEPLOYMENT.md](DEPLOYMENT.md) for complete guide.

### GitHub Actions (Free Tier)

Uses the included workflow at [.github/workflows/collect.yml](.github/workflows/collect.yml).

**Required Secrets** (automatically configured by CloudFormation):
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION` (e.g., us-east-1)
- `S3_BUCKET` (your bucket name)

**Cost**: Free (uses ~60% of 2,000 minutes/month free tier)

### AWS Lambda

Deploy as container image with EventBridge schedule:

```bash
# Build and push to ECR
aws ecr create-repository --repository-name ibkr-collector
docker build -t ibkr-collector .
docker tag ibkr-collector:latest $AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/ibkr-collector:latest
docker push $AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/ibkr-collector:latest

# Create Lambda function
aws lambda create-function \
  --function-name ibkr-collector \
  --package-type Image \
  --code ImageUri=$AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/ibkr-collector:latest \
  --role arn:aws:iam::$AWS_ACCOUNT:role/lambda-s3-role \
  --timeout 300 \
  --memory-size 512 \
  --environment Variables={S3_BUCKET=your-bucket}

# Create EventBridge schedule (every 15 minutes)
aws events put-rule \
  --name ibkr-collector-schedule \
  --schedule-expression "rate(15 minutes)"

aws events put-targets \
  --rule ibkr-collector-schedule \
  --targets "Id"="1","Arn"="arn:aws:lambda:$AWS_REGION:$AWS_ACCOUNT:function:ibkr-collector"
```

**Cost**: $0.00/month (within 1M requests + 400K GB-seconds free tier)

### AWS ECS/Fargate

Run as scheduled task:

```bash
# Create task definition
aws ecs register-task-definition --cli-input-json file://task-definition.json

# Create EventBridge schedule
aws events put-rule \
  --name ibkr-collector-schedule \
  --schedule-expression "rate(15 minutes)"

aws events put-targets \
  --rule ibkr-collector-schedule \
  --targets file://ecs-target.json
```

**Cost**: ~$3-5/month (Fargate spot pricing)

### Local Cron

```bash
# Add to crontab (runs every 15 minutes)
*/15 * * * * cd /path/to/ibkr-borrow-collector && docker run --rm -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY ibkr-collector --s3-bucket your-bucket
```

## Reconstruction

To reconstruct original data from deltas:

```bash
# Download baseline and deltas
aws s3 cp s3://bucket/ibkr/borrow/2026-01-16/usa-20260116_120000.txt.gz baseline.gz
aws s3 cp s3://bucket/ibkr/borrow/2026-01-16/usa-20260116_121500.xdelta delta1.xdelta
aws s3 cp s3://bucket/ibkr/borrow/2026-01-16/usa-20260116_123000.xdelta delta2.xdelta

# Decompress baseline
gunzip baseline.gz

# Apply deltas sequentially
xdelta3 -d -s baseline.txt delta1.xdelta reconstructed1.txt
xdelta3 -d -s reconstructed1.txt delta2.xdelta reconstructed2.txt

# Result: reconstructed2.txt contains data from 12:30
```

## Configuration

### Command-Line Options

```
--ftp-host HOST          FTP server (default: ftp2.interactivebrokers.com)
--ftp-user USER          FTP username (default: shortstock)
--ftp-pass PASS          FTP password (default: empty)
--s3-bucket BUCKET       S3 bucket name (required)
--s3-prefix PREFIX       S3 key prefix (default: ibkr/borrow)
--dry-run                Download only, skip S3 upload
--test-connection        Test FTP connection and exit
--log-json               Output structured JSON logs
--cache-dir DIR          Baseline cache directory (default: ~/.ibkr-baselines)
```

### Environment Variables

- `FTP_USER`: Override default FTP username
- `FTP_PASS`: Override default FTP password
- `AWS_ACCESS_KEY_ID`: AWS credentials
- `AWS_SECRET_ACCESS_KEY`: AWS credentials
- `AWS_DEFAULT_REGION`: AWS region

## Storage & Cost

### Storage Requirements

**Without Compression** (baselines only):
- 7 markets × 600 KB × 96 snapshots/day = 403 MB/day
- Annual: 147 GB/year = $3.38/year

**With Delta Compression** (98% savings):
- Baselines: 7 markets × 600 KB × 24/day = 10 MB/day
- Deltas: 7 markets × 250 bytes × 72/day = 0.12 MB/day
- **Total: 3.7 GB/year = $0.85/year**

### AWS Costs (Monthly)

| Service | Usage | Cost |
|---------|-------|------|
| S3 Storage | 310 MB average | $0.007 |
| S3 PUT Requests | 2,880 uploads | $0.014 |
| Lambda (optional) | 2,880 invocations | $0.000 (free tier) |
| Lambda Compute | 64,800 GB-seconds | $0.000 (free tier) |
| **Total** | | **$0.02/month** |

**Annual**: ~$0.25/year infrastructure + $0.85/year storage = **$1.10/year total**

## Markets & Coverage

| Market | File | Stocks | Market Hours (Local) |
|--------|------|--------|---------------------|
| USA | usa.txt | ~17,000 | 9:30 AM - 4:00 PM ET |
| UK | british.txt | ~2,000 | 8:00 AM - 4:30 PM GMT |
| Germany | germany.txt | ~1,500 | 9:00 AM - 5:30 PM CET |
| Switzerland | swiss.txt | ~300 | 9:00 AM - 5:30 PM CET |
| Italy | italy.txt | ~400 | 9:00 AM - 5:30 PM CET |
| Japan | japan.txt | ~3,500 | 9:00 AM - 3:00 PM JST |
| Hong Kong | hongkong.txt | ~2,000 | 9:30 AM - 4:00 PM HKT |

## Why Delta Compression?

Traditional approach stores full snapshots every 15 minutes:
- 600 KB × 96 snapshots/day = 57.6 MB/day per market
- $13/year for USA market alone

**Delta compression advantages**:
- **98.8% storage reduction**: 250 bytes vs 600 KB per snapshot
- **Fast reconstruction**: Apply deltas in milliseconds
- **Change detection**: Only upload when data changes
- **Hourly baselines**: Quick access without long delta chains
- **Corruption resilient**: Baselines provide recovery points

## Performance

| Metric | Value |
|--------|-------|
| Container size | ~50 MB |
| Cold start | <1 second |
| FTP download | 5-10 seconds |
| Delta creation | 100-200 ms |
| S3 upload (delta) | <1 second |
| Total runtime | 10-15 seconds |

## Data Quality

The collector includes several quality checks:

1. **MD5 Verification**: Compares against IBKR-provided checksums
2. **Change Detection**: Only uploads when content changes
3. **Reconstruction Verification**: Tests delta chain integrity
4. **Fallback to Baseline**: Auto-recovery on delta failures

## Use Cases

- **Quantitative Trading**: Historical borrow rate analysis
- **Risk Management**: Short squeeze prediction models
- **Market Research**: Borrow availability trends
- **Academic Research**: Market microstructure studies
- **Compliance**: Short interest reporting validation

## Limitations

- **Public FTP Access**: Data may lag ~15 minutes behind live rates
- **No Intraday History**: IBKR FTP only shows current snapshot
- **Market-Specific Files**: No cross-market aggregation
- **Share Counts**: Capped at ">10000000" for high availability

## Contributing

Contributions welcome! Areas for improvement:

- [ ] Support for additional markets (Australia, Canada, Nordic)
- [ ] Real-time alerting on rate changes
- [ ] Data validation against third-party sources
- [ ] Prometheus metrics exporter
- [ ] Terraform/CDK deployment templates

## License

Dual-licensed under:
- Apache License 2.0 ([LICENSE-APACHE-2.0](LICENSE-APACHE-2.0))
- GNU General Public License v3.0 ([LICENSE-GPL-3.0](LICENSE-GPL-3.0))

Choose the license that best fits your use case.

## Data Source

Data is collected from Interactive Brokers' public FTP server:
- **Server**: ftp2.interactivebrokers.com
- **Access**: Public (username: shortstock, no password)
- **Update Frequency**: Every 15 minutes during market hours
- **Official Docs**: [IBKR Short Stock Availability](https://www.interactivebrokers.com/en/index.php?f=26662)

## Acknowledgments

- Interactive Brokers for providing free public borrow data
- xdelta3 project for binary diff compression
- AWS for generous free tier limits

---

**Note**: This is an independent data collector. Not affiliated with or endorsed by Interactive Brokers.
