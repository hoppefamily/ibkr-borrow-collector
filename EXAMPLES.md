# Examples

## Basic Usage

### Test FTP Connection

```bash
python collector.py --test-connection --dry-run
```

### Dry Run (Download Only)

```bash
python collector.py --dry-run
```

### Collect and Upload to S3

```bash
python collector.py \
  --s3-bucket my-bucket \
  --s3-prefix ibkr/borrow
```

### JSON Logging

```bash
python collector.py \
  --s3-bucket my-bucket \
  --log-json > collection-log.json
```

## Docker Examples

### Build and Run

```bash
docker build -t ibkr-collector .

docker run --rm \
  -e AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY \
  -e AWS_DEFAULT_REGION=us-east-1 \
  ibkr-collector \
  --s3-bucket my-bucket
```

### Docker Compose

```bash
# Create .env file
cat > .env << EOF
AWS_ACCESS_KEY_ID=your-key
AWS_SECRET_ACCESS_KEY=your-secret
AWS_DEFAULT_REGION=us-east-1
S3_BUCKET=my-bucket
EOF

# Run
docker-compose up
```

## Data Reconstruction

### Reconstruct from Baseline + Delta

```python
#!/usr/bin/env python3
import gzip
import subprocess
import boto3

s3 = boto3.client('s3')
bucket = 'my-bucket'

# Download baseline
s3.download_file(
    bucket, 
    'ibkr/borrow/2026-01-16/usa-20260116_120000.txt.gz',
    'baseline.gz'
)

# Download delta
s3.download_file(
    bucket,
    'ibkr/borrow/2026-01-16/usa-20260116_121500.xdelta',
    'delta.xdelta'
)

# Decompress baseline
with gzip.open('baseline.gz', 'rb') as f_in:
    with open('baseline.txt', 'wb') as f_out:
        f_out.write(f_in.read())

# Apply delta
subprocess.run([
    'xdelta3', '-d',
    '-s', 'baseline.txt',
    'delta.xdelta',
    'reconstructed.txt'
])

# Read reconstructed data
with open('reconstructed.txt', 'r') as f:
    data = f.read()
    print(f"Reconstructed {len(data)} bytes")
```

### Reconstruct Delta Chain

```python
#!/usr/bin/env python3
import gzip
import subprocess
import boto3

def reconstruct_from_chain(bucket, date, market, target_time):
    """Reconstruct data from baseline + delta chain."""
    s3 = boto3.client('s3')
    prefix = f'ibkr/borrow/{date}'
    
    # List all files for this market
    response = s3.list_objects_v2(Bucket=bucket, Prefix=f'{prefix}/{market}')
    files = sorted([obj['Key'] for obj in response['Contents']])
    
    # Find baseline and deltas up to target time
    baseline = None
    deltas = []
    
    for file in files:
        timestamp = file.split('-')[1].split('.')[0]
        
        if file.endswith('.gz') and '.xdelta' not in file:
            if timestamp <= target_time:
                baseline = file
        elif file.endswith('.xdelta'):
            if timestamp <= target_time:
                deltas.append((timestamp, file))
    
    if not baseline:
        raise ValueError("No baseline found")
    
    # Download and decompress baseline
    s3.download_file(bucket, baseline, 'baseline.gz')
    with gzip.open('baseline.gz', 'rb') as f_in:
        with open('current.txt', 'wb') as f_out:
            f_out.write(f_in.read())
    
    # Apply deltas in order
    deltas.sort()
    for i, (ts, delta_key) in enumerate(deltas):
        s3.download_file(bucket, delta_key, f'delta{i}.xdelta')
        subprocess.run([
            'xdelta3', '-d',
            '-s', 'current.txt',
            f'delta{i}.xdelta',
            'next.txt'
        ])
        subprocess.run(['mv', 'next.txt', 'current.txt'])
    
    with open('current.txt', 'r') as f:
        return f.read()

# Example usage
data = reconstruct_from_chain(
    bucket='my-bucket',
    date='2026-01-16',
    market='usa',
    target_time='20260116_123000'
)

print(f"Reconstructed {len(data)} bytes at 12:30")
```

## AWS Lambda Handler

```python
#!/usr/bin/env python3
import os
import sys
import json
from collector import main

def lambda_handler(event, context):
    """AWS Lambda handler for collector."""
    
    # Set command-line arguments
    bucket = os.environ['S3_BUCKET']
    prefix = os.environ.get('S3_PREFIX', 'ibkr/borrow')
    
    sys.argv = [
        'collector.py',
        '--s3-bucket', bucket,
        '--s3-prefix', prefix,
        '--log-json'
    ]
    
    # Run collector
    try:
        exit_code = main()
        
        return {
            'statusCode': 200 if exit_code == 0 else 500,
            'body': json.dumps({
                'message': 'Collection completed',
                'exit_code': exit_code
            })
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'body': json.dumps({
                'message': 'Collection failed',
                'error': str(e)
            })
        }
```

## Parsing Data

### Python Parser

```python
#!/usr/bin/env python3
import gzip
from dataclasses import dataclass
from typing import List

@dataclass
class BorrowRate:
    symbol: str
    currency: str
    name: str
    contract_id: str
    isin: str
    rebate_rate: float
    fee_rate: float
    available: str

def parse_ibkr_file(filepath: str) -> List[BorrowRate]:
    """Parse IBKR borrow rate file."""
    rates = []
    
    # Handle gzipped files
    if filepath.endswith('.gz'):
        with gzip.open(filepath, 'rt') as f:
            lines = f.readlines()
    else:
        with open(filepath, 'r') as f:
            lines = f.readlines()
    
    for line in lines:
        # Skip header
        if line.startswith('#'):
            continue
        
        parts = line.strip().split('|')
        if len(parts) != 8:
            continue
        
        rates.append(BorrowRate(
            symbol=parts[0],
            currency=parts[1],
            name=parts[2],
            contract_id=parts[3],
            isin=parts[4],
            rebate_rate=float(parts[5]),
            fee_rate=float(parts[6]),
            available=parts[7]
        ))
    
    return rates

# Example usage
rates = parse_ibkr_file('usa-20260116_120000.txt.gz')

# Find expensive borrows
expensive = [r for r in rates if r.fee_rate > 1.0]
print(f"Found {len(expensive)} stocks with >1% borrow rate")

for rate in sorted(expensive, key=lambda r: r.fee_rate, reverse=True)[:10]:
    print(f"{rate.symbol:6s} {rate.fee_rate:6.2f}% ({rate.available:>10s} available)")
```

### Comparing Snapshots

```python
#!/usr/bin/env python3
import gzip

def compare_snapshots(file1: str, file2: str):
    """Compare two IBKR snapshots to find rate changes."""
    
    def load_rates(filepath):
        rates = {}
        with gzip.open(filepath, 'rt') as f:
            for line in f:
                if line.startswith('#'):
                    continue
                parts = line.strip().split('|')
                if len(parts) == 8:
                    rates[parts[0]] = float(parts[6])
        return rates
    
    rates1 = load_rates(file1)
    rates2 = load_rates(file2)
    
    # Find changes
    changes = []
    for symbol in rates1:
        if symbol in rates2:
            diff = rates2[symbol] - rates1[symbol]
            if abs(diff) > 0.01:  # More than 1 basis point
                changes.append((symbol, rates1[symbol], rates2[symbol], diff))
    
    # Print top changes
    changes.sort(key=lambda x: abs(x[3]), reverse=True)
    print(f"Top 20 rate changes:")
    print(f"{'Symbol':<8} {'Old Rate':<10} {'New Rate':<10} {'Change':<10}")
    print("-" * 48)
    
    for symbol, old, new, diff in changes[:20]:
        print(f"{symbol:<8} {old:>9.2f}% {new:>9.2f}% {diff:>+9.2f}%")

# Example usage
compare_snapshots(
    'usa-20260116_120000.txt.gz',
    'usa-20260116_123000.txt.gz'
)
```

## CI/CD Integration

### GitHub Actions Custom Schedule

```yaml
name: Custom Collection Schedule

on:
  schedule:
    # Every 5 minutes during US market hours
    - cron: '*/5 14-21 * * 1-5'  # 9:30 AM - 4:00 PM ET
  workflow_dispatch:

jobs:
  collect:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Build and run collector
        env:
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          AWS_DEFAULT_REGION: us-east-1
          S3_BUCKET: ${{ secrets.S3_BUCKET }}
        run: |
          docker build -t collector .
          docker run --rm \
            -e AWS_ACCESS_KEY_ID \
            -e AWS_SECRET_ACCESS_KEY \
            -e AWS_DEFAULT_REGION \
            collector \
            --s3-bucket "$S3_BUCKET" \
            --log-json
```

### GitLab CI

```yaml
collect:
  image: docker:latest
  services:
    - docker:dind
  script:
    - docker build -t collector .
    - docker run --rm
        -e AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID
        -e AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY
        -e AWS_DEFAULT_REGION=$AWS_DEFAULT_REGION
        collector
        --s3-bucket $S3_BUCKET
        --log-json
  only:
    - schedules
```
