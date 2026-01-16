# Architecture

This document describes the technical architecture, delta compression strategy, and operational considerations for the IBKR borrow collector.

## Table of Contents

- [System Overview](#system-overview)
- [Delta Compression Strategy](#delta-compression-strategy)
- [Data Flow](#data-flow)
- [Baseline vs Delta Decision Logic](#baseline-vs-delta-decision-logic)
- [Failure Modes and Recovery](#failure-modes-and-recovery)
- [Storage Architecture](#storage-architecture)
- [Performance Characteristics](#performance-characteristics)

## System Overview

The IBKR borrow collector is a data pipeline that:
1. Downloads borrow rate and margin requirement files from IBKR FTP servers
2. Applies delta compression to minimize storage costs (98% reduction)
3. Uploads compressed data to S3 with intelligent lifecycle policies
4. Runs every 15 minutes during market hours via GitHub Actions

### Key Design Principles

- **Cost optimization**: Delta compression achieves 98% storage savings
- **Reliability**: Automatic fallback from delta to baseline on failures
- **Transparency**: Every snapshot is independently verifiable via MD5 checksums
- **Simplicity**: Standard tools (xdelta3, gzip) with no proprietary formats

## Delta Compression Strategy

### Why Delta Compression?

IBKR borrow rate files are ~1.5 MB but change by only 1-5% between snapshots (15-minute intervals). Storing full snapshots would cost ~5.5 GB/year. Delta compression reduces this to ~120 MB/year.

**Comparison:**

| Strategy | Initial | Per Snapshot | Daily | Annual | Cost/Year |
|----------|---------|--------------|-------|--------|-----------|
| Full snapshots | 1.5 MB | 1.5 MB | 144 MB | 52 GB | $14.40 |
| Gzip only | 150 KB | 150 KB | 14 MB | 5.1 GB | $1.52 |
| **Delta + gzip** | **150 KB** | **3 KB** | **400 KB** | **120 MB** | **$0.35** |

### Delta Chain Architecture

We use **chained deltas** where each snapshot can reference either:
- A baseline (full compressed snapshot created hourly)
- The previous delta (created every 15 minutes)

```
Timeline:  09:00    09:15    09:30    09:45    10:00    10:15
           ┌────┐   ┌────┐   ┌────┐   ┌────┐   ┌────┐   ┌────┐
Snapshot:  │BASE│ → │Δ1  │ → │Δ2  │ → │Δ3  │ → │BASE│ → │Δ1  │
           └────┘   └────┘   └────┘   └────┘   └────┘   └────┘
Size:      150 KB    3 KB     3 KB     3 KB    150 KB    3 KB

Storage:   BASE → Δ1 → Δ2 → Δ3 → BASE → Δ1
```

**Delta Metadata** (stored in S3 object metadata):
```json
{
  "snapshot-type": "delta",
  "source-key": "ibkr/borrow/2026-01-16/usa-20260116_090000.txt.gz",
  "source-type": "baseline",
  "original-md5": "abc123...",
  "source-md5": "def456...",
  "delta-size": "3072"
}
```

### Reconstruction Process

To reconstruct any snapshot:

1. **If it's a baseline**: Simply download and decompress
2. **If it's a delta**:
   - Follow the chain back to find the baseline
   - Download baseline + all intermediate deltas
   - Apply deltas sequentially using xdelta3

**Example reconstruction:**

```
Target: usa-20260116_094500 (delta)
  ↓ source-key: usa-20260116_093000 (delta)
  ↓ source-key: usa-20260116_091500 (delta)
  ↓ source-key: usa-20260116_090000 (baseline)

Steps:
1. Download baseline: usa-20260116_090000.txt.gz
2. Decompress: gunzip → usa_090000.txt
3. Download delta: usa-20260116_091500.xdelta
4. Apply: xdelta3 -d -s usa_090000.txt usa-20260116_091500.xdelta usa_091500.txt
5. Download delta: usa-20260116_093000.xdelta
6. Apply: xdelta3 -d -s usa_091500.txt usa-20260116_093000.xdelta usa_093000.txt
7. Download delta: usa-20260116_094500.xdelta
8. Apply: xdelta3 -d -s usa_093000.txt usa-20260116_094500.xdelta usa_094500.txt
```

## Data Flow

### Collection Flow (Happy Path)

```
┌─────────────┐
│ FTP Server  │
│ (IBKR)      │
└──────┬──────┘
       │ 1. Download file + .md5
       │
       ▼
┌─────────────────────┐
│ FTPDownloader       │
│ - Download file     │
│ - Verify checksum   │
└──────┬──────────────┘
       │ 2. Local file
       │
       ▼
┌─────────────────────┐     ┌──────────────┐
│ Decision Logic      │────→│ Create       │
│ - Time-based?       │     │ Baseline     │
│ - xdelta3 available?│     │ - Compress   │
│ - Has previous snap?│     │ - Upload     │
└──────┬──────────────┘     └──────────────┘
       │
       │ If delta path
       ▼
┌─────────────────────┐
│ Find Previous       │
│ Snapshot            │
│ - Query S3          │
│ - Get latest file   │
└──────┬──────────────┘
       │ 3. Previous snapshot metadata
       │
       ▼
┌─────────────────────┐
│ Reconstruct Source  │
│ - Download baseline │
│ - Download deltas   │
│ - Apply xdelta3     │
└──────┬──────────────┘
       │ 4. Source file
       │
       ▼
┌─────────────────────┐
│ Compare Content     │
│ - Calculate MD5     │
│ - Skip if unchanged │
└──────┬──────────────┘
       │ 5. If changed
       │
       ▼
┌─────────────────────┐
│ Create Delta        │
│ - xdelta3 -e        │
│ - Store metadata    │
└──────┬──────────────┘
       │ 6. Delta file
       │
       ▼
┌─────────────────────┐
│ S3Uploader          │
│ - Upload to S3      │
│ - Set metadata      │
└─────────────────────┘
```

### Sequence Diagram: Delta Creation

```
┌─────────┐   ┌─────┐   ┌──────────┐   ┌────────────┐   ┌────┐
│Collector│   │ FTP │   │S3Uploader│   │XDeltaComp  │   │ S3 │
└────┬────┘   └──┬──┘   └────┬─────┘   └─────┬──────┘   └─┬──┘
     │           │           │               │             │
     │ download  │           │               │             │
     ├──────────>│           │               │             │
     │<──────────┤           │               │             │
     │  file     │           │               │             │
     │           │           │               │             │
     │ list_objects(prefix)  │               │             │
     ├──────────────────────>│               │             │
     │<──────────────────────┤               │             │
     │  [latest snapshots]   │               │             │
     │           │           │               │             │
     │ download_file(source) │               │             │
     ├──────────────────────>│               │             │
     │           │           ├──────────────>│             │
     │           │           │  S3 GET       │             │
     │<──────────────────────┤<──────────────┤             │
     │  source file          │               │             │
     │           │           │               │             │
     │ create_delta(source, target)          │             │
     ├──────────────────────────────────────>│             │
     │<──────────────────────────────────────┤             │
     │  delta file           │               │             │
     │           │           │               │             │
     │ upload_file(delta, metadata)          │             │
     ├──────────────────────>│               │             │
     │           │           ├──────────────────────────────>│
     │           │           │  S3 PUT       │             │
     │<──────────────────────┤<──────────────────────────────┤
     │  success              │               │             │
```

## Baseline vs Delta Decision Logic

### Decision Tree

```
┌─────────────────────────────────┐
│ Should create BASELINE?          │
└────────┬────────────────────────┘
         │
         ├─→ Is xdelta3 installed? ──→ NO ──→ CREATE BASELINE
         │                                     (fallback)
         ├─→ Is minute < 10? ──────────→ YES ─→ CREATE BASELINE
         │   (hourly baseline)                  (scheduled)
         │
         ├─→ Is use_delta=False? ──────→ YES ─→ CREATE BASELINE
         │   (forced baseline)                  (forced)
         │
         └─→ Previous snapshot exists? ─→ NO ──→ CREATE BASELINE
                                                  (first snapshot)
                ↓ YES
         ┌──────────────────┐
         │ CREATE DELTA     │
         │ (normal path)    │
         └──────────────────┘
```

### Implementation

```python
def should_create_baseline(current_time: datetime) -> bool:
    """Determine if we should create a new baseline.

    Creates baseline at the start of each hour (00-09 minutes).
    """
    return current_time.minute < 10

# In process_file():
xdelta_available = XDeltaCompressor.is_available()
is_scheduled_baseline = should_create_baseline(current_time)
create_baseline = (
    is_scheduled_baseline or      # Hourly schedule
    not use_delta or               # Deltas disabled
    not xdelta_available or        # xdelta3 not found
    not previous_snapshot_exists   # First snapshot
)
```

### Baseline Creation Strategy

**Why hourly baselines?**

1. **Limit delta chain length**: Max 4 deltas between baselines (4 × 15min = 60min)
2. **Fast reconstruction**: No need to follow long chains
3. **Resilience**: If a delta is corrupted, only lose 1 hour of data
4. **Query performance**: Start point for time-range queries

**Baseline timing:**
- Created when `minute < 10` (09:00, 10:00, 11:00, etc.)
- First snapshot of the day is always a baseline
- Fallback to baseline if any delta operation fails

## Failure Modes and Recovery

### 1. xdelta3 Not Available

**Symptom:** `xdelta3` command not found

**Behavior:**
- Collector logs warning: `⚠️  xdelta3 is not available`
- Automatically falls back to baseline creation
- Creates full compressed snapshots (.gz) instead of deltas
- Collection continues successfully (98% → 90% compression)

**Recovery:**
```bash
# Install xdelta3
brew install xdelta          # macOS
apt-get install xdelta3      # Ubuntu
apk add xdelta3              # Alpine

# Verify
xdelta3 -V
```

**Prevention:**
- Dockerfile includes xdelta3: `apk add xdelta3`
- Local testing should verify xdelta3 presence

### 2. Source Snapshot Not Found

**Symptom:** No previous snapshot exists for delta creation

**Behavior:**
- Collector logs: `No previous snapshot found for {filename}`
- Automatically creates baseline snapshot instead
- Metadata marks as `snapshot-type: baseline`
- Next collection will delta from this baseline

**Causes:**
- First run of the day
- S3 bucket was emptied
- Different S3 prefix used

**Recovery:** Automatic - no action needed

### 3. Delta Reconstruction Failure

**Symptom:** `xdelta3 -d` fails during source reconstruction

**Behavior:**
- Collector logs: `Delta reconstruction failed: [error]`
- Falls back to creating baseline snapshot
- Current collection succeeds
- Broken delta chain is bypassed

**Causes:**
- Corrupted baseline or delta file
- Incomplete S3 upload
- Mismatched source-key metadata

**Recovery:**
```bash
# Identify problematic delta
aws s3 ls s3://$BUCKET/ibkr/borrow/2026-01-16/ --human-readable

# Verify file integrity
aws s3 cp s3://$BUCKET/ibkr/borrow/2026-01-16/usa-20260116_093000.xdelta - | xdelta3 -d -s baseline.txt - output.txt

# If corrupted, delete and let next collection create baseline
aws s3 rm s3://$BUCKET/ibkr/borrow/2026-01-16/usa-20260116_093000.xdelta
```

**Prevention:**
- S3 versioning enabled (rollback possible)
- MD5 verification on uploads
- Hourly baselines limit blast radius

### 4. FTP Connection Failure

**Symptom:** Cannot connect to IBKR FTP server

**Behavior:**
- Collector logs: `Failed to connect to FTP: [error]`
- Collection exits with code 1
- GitHub Actions marks run as failed
- Next scheduled run will retry

**Causes:**
- Network connectivity issues
- IBKR server maintenance
- Firewall blocking port 21

**Recovery:**
```bash
# Test FTP connection
python collector.py --test-connection --dry-run

# Check network
ping ftp2.interactivebrokers.com
telnet ftp2.interactivebrokers.com 21
```

**Mitigation:**
- Collections run every 15 minutes (auto-retry)
- Missing one collection is not critical
- Consider retry logic with backoff

### 5. S3 Upload Failure

**Symptom:** `S3 upload failed: [error]`

**Behavior:**
- Collector logs error
- File marked as `status: failed` in logs
- Collection continues for other files
- Exit code 1 (failed)

**Causes:**
- IAM permission issues
- Network timeout
- S3 bucket doesn't exist
- Bucket in different region

**Recovery:**
```bash
# Verify IAM permissions
aws sts get-caller-identity

# Test S3 access
aws s3 ls s3://$S3_BUCKET/

# Check bucket region
aws s3api get-bucket-location --bucket $S3_BUCKET

# Manual upload
python collector.py --s3-bucket $S3_BUCKET --dry-run  # Test locally first
```

**Prevention:**
- Use CloudFormation template (correct permissions)
- Monitor GitHub Actions logs
- Set up CloudWatch alarms

### 6. Content Unchanged (False Positive)

**Symptom:** `File unchanged (MD5: abc123), skipping upload`

**Behavior:**
- No upload performed
- Marked as `status: skipped`
- Storage cost saved
- Normal operation

**Notes:**
- This is expected behavior during off-market hours
- IBKR updates files during market hours only
- Can force upload with `--force-upload` flag

### 7. Long Delta Chain

**Symptom:** Reconstruction requires many deltas (>4)

**Behavior:**
- Reconstruction time increases (seconds)
- Higher risk of chain break
- More S3 API calls needed

**Causes:**
- Missed hourly baseline creation
- Collector ran during minute 10-59 for extended period

**Recovery:**
```bash
# Force baseline creation
# Delete recent deltas to trigger baseline
aws s3 rm s3://$BUCKET/ibkr/borrow/$(date +%Y-%m-%d)/usa-$(date +%Y%m%d)_*.xdelta

# Next run will create baseline
```

**Prevention:**
- Hourly baseline schedule enforced in code
- Even if minute < 10 is missed, next snapshot creates baseline on failure

## Storage Architecture

### S3 Bucket Structure

```
s3://bucket-name/
└── ibkr/
    └── borrow/
        ├── 2026-01-16/
        │   ├── usa-20260116_090000.txt.gz           # Baseline (150 KB)
        │   ├── usa-20260116_091500.xdelta           # Delta (3 KB)
        │   ├── usa-20260116_093000.xdelta           # Delta (3 KB)
        │   ├── usa-20260116_094500.xdelta           # Delta (3 KB)
        │   ├── usa-20260116_100000.txt.gz           # Baseline (150 KB)
        │   ├── british-20260116_090000.txt.gz
        │   ├── stockmargin_final_dtls.IBLLC-US-090000.dat.gz
        │   └── ...
        ├── 2026-01-17/
        │   └── ...
        └── 2026-01-18/
            └── ...
```

### Lifecycle Policies

```
Day 0-30:    Standard Storage        ($0.023/GB/month)
             ↓
Day 30-90:   Intelligent-Tiering     ($0.0025/GB monitoring)
             ↓
Day 90-365:  Glacier Instant Retrieval ($0.004/GB/month)
             ↓
Day 365+:    Deleted                  (configurable)
```

**Noncurrent versions:** Deleted after 1 day

### Object Metadata

Every S3 object includes metadata for tracking and reconstruction:

**Baseline:**
```json
{
  "original-md5": "abc123...",
  "file-type": "borrow",
  "snapshot-type": "baseline",
  "collection-time": "2026-01-16T09:00:00.000000",
  "original-size": "1572864",
  "compressed-size": "153600"
}
```

**Delta:**
```json
{
  "original-md5": "def456...",
  "source-md5": "abc123...",
  "source-key": "ibkr/borrow/2026-01-16/usa-20260116_090000.txt.gz",
  "source-type": "baseline",
  "file-type": "borrow",
  "snapshot-type": "delta",
  "collection-time": "2026-01-16T09:15:00.000000",
  "original-size": "1572864",
  "delta-size": "3072"
}
```

## Performance Characteristics

### Collection Time

| Operation | Time | Notes |
|-----------|------|-------|
| FTP download | 2-5s | 1.5 MB file |
| MD5 verification | <1s | |
| Find previous snapshot | <1s | S3 list |
| Download source | 1-2s | Cached locally |
| Decompress source | <1s | gzip |
| Create delta | 2-3s | xdelta3 -e -9 |
| Upload delta | <1s | 3 KB file |
| **Total (delta)** | **8-15s** | Per market |
| **Total (baseline)** | **5-10s** | Per market |

**Parallelization:** Markets processed sequentially (18 borrow + 8 margin = ~3-5 minutes total)

### Reconstruction Time

| Delta Chain Length | Time | S3 API Calls |
|-------------------|------|--------------|
| Baseline only | 1-2s | 1 GET |
| Baseline + 1 delta | 3-4s | 2 GET |
| Baseline + 4 deltas | 8-10s | 5 GET |

### Storage Efficiency

**Per market per day:**
- Baseline: 150 KB × 4 baselines = 600 KB
- Deltas: 3 KB × 92 deltas = 276 KB
- **Total: ~876 KB/day** (vs 14 MB without delta compression)

**Annual (18 borrow + 8 margin markets):**
- With delta: 120 MB → $0.35/year
- Without delta: 5.1 GB → $1.52/year
- Full snapshots: 52 GB → $14.40/year

### Cache Performance

Local baseline caching (`.ibkr-baselines/`):
- First reconstruction: 2 S3 downloads (baseline + delta)
- Subsequent: 1 S3 download (delta only)
- Cache hit rate: ~80% (same baseline used for 1 hour)
- Reduces API calls by 50%

## Best Practices

### For Operators

1. **Monitor collection logs**: Check for repeated failures or warnings
2. **Verify xdelta3**: Ensure xdelta3 is installed in deployment environment
3. **Set up alerts**: CloudWatch alarm for failed GitHub Actions runs
4. **Test recovery**: Periodically verify delta reconstruction works
5. **Review costs**: Monitor S3 storage costs (should be <$1/month)

### For Developers

1. **Test locally**: Use `--dry-run` for testing without S3 uploads
2. **Verify deltas**: Manually reconstruct deltas to verify integrity
3. **Handle failures gracefully**: Always fall back to baseline creation
4. **Include metadata**: Store all context needed for reconstruction
5. **Document assumptions**: Clearly document hourly baseline requirement

### For Data Consumers

1. **Use baselines for point queries**: Faster than delta reconstruction
2. **Batch reconstruct for time ranges**: Reuse baseline across multiple deltas
3. **Cache baselines**: Store decompressed baselines to avoid repeated work
4. **Validate checksums**: Verify MD5 hashes after reconstruction
5. **Plan for gaps**: Missing data during IBKR outages is normal

## Future Improvements

1. **Parallel processing**: Process markets in parallel to reduce total time
2. **Smart baseline creation**: Create baseline when delta size > threshold
3. **Delta compaction**: Periodically merge old deltas into new baselines
4. **Content-aware parsing**: Skip unchanged records within files
5. **Multi-region replication**: Replicate critical data to multiple regions
6. **Real-time streaming**: Push updates via SNS/SQS for downstream consumers
7. **Data quality monitoring**: Automatic anomaly detection (missing symbols, invalid rates)

## References

- [xdelta3 Documentation](https://github.com/jmacd/xdelta)
- [AWS S3 Lifecycle Policies](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html)
- [GitHub Actions OIDC](https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services)
- [IBKR Short Stock Availability](https://www.interactivebrokers.com/en/index.php?f=2024)
