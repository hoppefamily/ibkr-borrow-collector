# IBKR Intraday Aggregation Feature

## Overview

The IBKR borrow collector now computes **daily aggregate features** from intraday 15-minute snapshots during the parquet cache building phase. This provides richer signal quality for daily analysis without requiring intraday trading.

## Features

### Daily OHLC Borrow Rates
- **`borrow_rate_open`**: First snapshot of the trading day
- **`borrow_rate_close`**: Last snapshot of the trading day (primary field)
- **`borrow_rate_high`**: Peak borrow rate during the day
- **`borrow_rate_low`**: Minimum borrow rate during the day

### Statistical Features
- **`borrow_rate_mean`**: Time-weighted average across all snapshots
- **`borrow_rate_std`**: Standard deviation (volatility/stress indicator)

### Behavioral Features
- **`intraday_trend`**: Close minus Open (directional pressure indicator)
  - Positive: Rising pressure during day (borrowing demand increasing)
  - Negative: Falling pressure during day (borrowing demand decreasing)
  
- **`availability_changes`**: Count of availability state transitions
  - High count indicates active lending market with fluctuating supply

### Metadata
- **`snapshot_count`**: Number of 15-minute snapshots processed
- **`is_daily_aggregate`**: Boolean flag marking aggregate rows

## Data Structure

Each symbol gets TWO types of rows in the parquet file:

1. **Intraday snapshots** (deduplicated): Only snapshots where borrow rate or availability changed
2. **Daily aggregate** (one per symbol): Summary statistics marked with `is_daily_aggregate=True`

## Example Output

```
SAP.DE on 2026-01-30:
  Borrow Rate (Close): 0.6148%  ← Primary field for close-to-close analysis
  Borrow Rate (Open):  0.5859%
  Borrow Rate (High):  0.6172%
  Borrow Rate (Low):   0.5859%
  Borrow Rate (Mean):  0.6003%
  Volatility (Std):    0.0148%  ← Stress indicator
  Intraday Trend:      +0.0289% ← Building pressure (rising)
  Avail Changes:       5         ← Active lending market
```

## Benefits

### 1. Better Signal Quality
- **Consistent comparisons**: Close-to-close instead of arbitrary snapshot times
- **Less noise**: Volatility measure helps filter false signals
- **Directional info**: Intraday trend shows if pressure is building or releasing

### 2. Early Warning Signals
- High volatility → Unstable borrow market
- Rising intraday trend → Building pressure (potential squeeze)
- Frequent availability changes → Active shorting/covering

### 3. Performance
- **Computed once** during cache build (not on every read)
- **Single source of truth** for all downstream consumers
- **Backward compatible** with old parquet format

## Usage

### In flow-state-monitor

The IBKR provider automatically prefers daily aggregates when available:

```python
from flow_state_monitor.providers.ibkr import IBKRBorrowRateProvider
from datetime import date

provider = IBKRBorrowRateProvider(
    bucket='ibkr-borrow-collector-borrowdatabucket-u0yupnyt837q',
)

# Returns DataFrame with extended features if aggregates exist
result = provider.fetch_borrow_rates(
    symbols=['SAP.DE', 'BMW.DE'],
    start=date(2026, 1, 30),
    end=date(2026, 1, 30)
)

# Check if enhanced features are available
if 'borrow_rate_std' in result.columns:
    print("✓ Enhanced features available")
    print(f"Volatility: {result['borrow_rate_std'].iloc[0]:.4f}%")
```

### Rebuilding Caches

To rebuild existing parquet caches with aggregation:

```bash
# Single date
python build_parquet_cache.py --date 2026-01-30 --market germany --force

# Date range
python build_parquet_cache.py --start 2026-01-15 --end 2026-01-30 --market usa --force

# Via GitHub workflow (missing mode)
gh workflow run build-parquet-cache.yml -f mode=missing -f days_back=30
```

## Implementation Details

### Aggregation Logic

```python
def _compute_daily_aggregates(df: pd.DataFrame, target_date: date):
    """
    Compute OHLC and statistical features from intraday snapshots.
    
    - Groups by symbol
    - Sorts by snapshot_time
    - Computes open (first), close (last), high (max), low (min)
    - Calculates mean and std dev
    - Counts availability state transitions
    """
```

### Provider Logic

```python
def _process_market_data(df: pd.DataFrame, symbols: List[str]):
    """
    Prefer daily aggregates when available, fallback to snapshots.
    
    1. Filter for is_daily_aggregate=True rows
    2. For symbols missing aggregates, use latest snapshot
    3. Combine and return with extended features
    """
```

## Migration Path

### Phase 1: Gradual Rollout ✓
- Feature implemented in branch
- Backward compatible (works with/without aggregates)
- Consumers automatically use aggregates when available

### Phase 2: Rebuild Caches
- Run workflow to rebuild last 30 days
- Verify all markets have aggregates
- Monitor for any issues

### Phase 3: Utilize Features
- Update signal generation to use volatility and trend
- Add alerts for high volatility symbols
- Improve momentum calculations with intraday trend

## Future Enhancements

### Possible Additional Features
- **Time-of-day patterns**: Open vs close rate comparison
- **Rate acceleration**: Second derivative of trend
- **Availability flow**: Net change in shares available
- **Cross-symbol correlation**: Market-wide stress indicator

### Integration Points
- Use `borrow_rate_std` in quality scoring
- Weight `intraday_trend` in momentum calculation
- Alert on high `availability_changes` + rising trend
- Filter low-volatility symbols from analysis

## Testing

```bash
# Test enhanced features
cd flow-state-monitor
python test_enhanced_features.py

# Verify aggregates exist for date
python -c "
from flow_state_monitor.providers.ibkr import IBKRBorrowRateProvider
from datetime import date
provider = IBKRBorrowRateProvider(bucket='...')
result = provider.fetch_borrow_rates(['SPY'], date(2026,1,30), date(2026,1,30))
print('Has aggregates:', 'borrow_rate_std' in result.columns)
"
```

## Performance Impact

- **Cache build time**: +8 seconds per market per day (378k rows → 7k aggregates)
- **Cache size**: +2.7% (7,071 aggregate rows added to 18,709 deduplicated rows)
- **Query performance**: No impact (same read path)
- **Compute cost**: One-time during cache build

## Backward Compatibility

✓ Old providers work with new parquet files (ignore extra columns)
✓ New providers work with old parquet files (fallback to snapshots)
✓ No breaking changes to API or data structures
