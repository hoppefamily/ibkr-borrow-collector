# Data Quality Plan: Layered Approach for Collection Gaps

## Background

On **2026-02-02**, GitHub Actions experienced a 2-hour outage due to an Azure cloud infrastructure issue. This caused the IBKR borrow collector to miss **8 collection runs** (14:00-16:00 UTC).

### Impact
- **Duration:** 2 hours (8 missed 15-minute collection intervals)
- **Affected Markets:** All global markets (USA, Germany, UK, etc.)
- **Affected Symbols:** ~15,000 symbols
- **Data Completeness:** ~81% for the day (78/96 snapshots)
- **Root Cause:** Azure cloud outage (external dependency)

### Key Constraint
IBKR FTP does not retain historical data beyond a few hours, so **backfilling is not possible**. We must accept gaps and prevent them from creating false signals.

---

## Design Principle

**Priority: Prevent false signals over perfect data coverage**

Different severity levels require different handling:
- **SEVERE gaps:** Skip entirely (no data better than false data)
- **DEGRADED gaps:** Use with caution, fall back to more reliable sources
- **MINOR gaps:** Treat as complete

---

## Quality Tiers by Severity

### Tier 1: SEVERE (Skip Day)
- **Threshold:** < 50% of expected snapshots
- **Action:** Return `None` → no flow_state created
- **Rationale:** Too much missing data to be reliable
- **Dashboard:** Shows as gap (no data point)

### Tier 2: DEGRADED (Use with Caution)
- **Threshold:** 50-90% of expected snapshots
- **Action:** Return data with `data_quality: DEGRADED` flag
- **Flow monitor behavior:**
  - Use EOD close for daily momentum calculations
  - Skip intraday features (volatility, trend, availability changes)
  - Fall back to iBorrowDesk historic if available (more reliable single EOD point)
- **Dashboard:** Shows normally, optional `*` indicator

### Tier 3: MINOR (Use Normally)
- **Threshold:** 90-100% of expected snapshots
- **Action:** Treat as complete, no flag
- **Rationale:** Missing 1-10 snapshots out of 96 is negligible (~10% margin)

---

## Data Source-Specific Thresholds

### Intraday IBKR Data (Current)
- **Expected:** 96 snapshots/day (15-min intervals)
- **Thresholds:**
  - **SEVERE:** < 48 snapshots (< 50%) → Skip day
  - **DEGRADED:** 48-86 snapshots (50-90%) → Use with caution
  - **MINOR:** 87-96 snapshots (90-100%) → Treat as complete

**2026-02-02 outage:** 78/96 = 81% → **DEGRADED**

### Historic iBorrowDesk Data
- **Expected:** 1 snapshot/day (EOD only)
- **Thresholds:**
  - **SEVERE:** 0 snapshots → Skip day
  - **MINOR:** 1 snapshot → Complete (100%)
- **No DEGRADED tier:** Binary (have EOD or don't have EOD)

### Future iBorrowDesk Real-time (If Implemented)
- **Expected:** 1+ snapshots/day (sporadic updates when rate changes)
- **Thresholds:**
  - **SEVERE:** 0 snapshots → Skip day
  - **MINOR:** 1+ snapshots → Complete
- **Rationale:** Sporadic data by nature, can't expect 96 snapshots

---

## Implementation Plan

### Phase 1: Parquet Cache - Quality Detection

**Repository:** `ibkr-borrow-collector`
**File:** `build_parquet_cache.py`

#### 1.1 Quality Assessment Function
```python
def assess_data_quality(snapshot_count, data_source_type):
    """
    Assess quality based on data source expectations.

    Args:
        snapshot_count: Actual number of snapshots collected
        data_source_type: 'ibkr_intraday' | 'iborrowdesk_historic' | 'iborrowdesk_realtime'

    Returns:
        (quality_level, expected_snapshots)
        quality_level: 'SEVERE' | 'DEGRADED' | 'COMPLETE'
    """
    if data_source_type == 'ibkr_intraday':
        expected = 96
        if snapshot_count < 48:  # < 50%
            return 'SEVERE', expected
        elif snapshot_count < 87:  # 50-90%
            return 'DEGRADED', expected
        else:  # 90-100%
            return 'COMPLETE', expected

    elif data_source_type == 'iborrowdesk_historic':
        expected = 1
        if snapshot_count == 0:
            return 'SEVERE', expected
        else:
            return 'COMPLETE', expected

    elif data_source_type == 'iborrowdesk_realtime':
        expected = 1  # minimum
        if snapshot_count == 0:
            return 'SEVERE', expected
        else:
            return 'COMPLETE', expected

    return 'COMPLETE', snapshot_count  # Default
```

#### 1.2 Daily Aggregate Metadata
Store quality metadata in daily aggregate rows:
```python
{
    'date': '2026-02-02',
    'symbol': 'BMW.DE',
    'borrow_rate_close': 0.65,
    # ... other OHLC fields ...

    # Quality metadata
    'snapshot_count': 78,
    'expected_snapshots': 96,
    'data_source_type': 'ibkr_intraday',
    'data_quality': 'DEGRADED',  # SEVERE | DEGRADED | COMPLETE
    'completeness_ratio': 0.8125,
    'is_daily_aggregate': True
}
```

#### 1.3 Gap Period Tracking (Optional)
For debugging, store gap periods:
```python
'gap_periods': '2026-02-02T14:00:00Z/2026-02-02T16:00:00Z'  # ISO 8601 interval
```

---

### Phase 2: Flow State Monitor - Intelligent Handling

#### 2.1 Multi Borrow Provider - Smart Fallback

**Repository:** `flow-state-monitor`
**File:** `src/flow_state_monitor/providers/borrow/multi.py`

Current priority: IBKR → iBorrow Historic → iBorrowDesk

**Revised behavior:**
```python
def get_borrow_data(self, symbol, date):
    """
    Get borrow data with intelligent quality-based fallback.
    """
    # Try IBKR first
    ibkr_data = self._ibkr_provider.get(symbol, date)

    if ibkr_data:
        quality = ibkr_data.get('data_quality', 'COMPLETE')

        if quality == 'SEVERE':
            # IBKR data unusable, fall back to historic
            logger.warning(f"{symbol} {date}: IBKR severe quality, trying historic")
            return self._historic_provider.get(symbol, date)

        elif quality == 'DEGRADED':
            # IBKR data usable but check if historic is available
            historic_data = self._historic_provider.get(symbol, date)

            if historic_data:
                # Historic has complete EOD - more reliable single point
                logger.info(f"{symbol} {date}: Using historic EOD instead of degraded IBKR")
                return historic_data
            else:
                # No historic available, use degraded IBKR
                logger.warning(f"{symbol} {date}: Using degraded IBKR data (no historic available)")
                return ibkr_data

        else:  # COMPLETE
            return ibkr_data

    # IBKR not available, fall back to historic
    return self._historic_provider.get(symbol, date)
```

**Key insight:** When IBKR is DEGRADED, prefer iBorrowDesk historic (1 reliable EOD snapshot) over incomplete intraday data.

#### 2.2 IBKR Provider - Pass Quality Metadata

**Repository:** `flow-state-monitor`
**File:** `src/flow_state_monitor/providers/borrow/ibkr.py`

```python
def get_borrow_data(self, symbol, date):
    """Read from Parquet and pass through quality metadata."""
    row = self._read_parquet(symbol, date)

    if not row:
        return None

    # Extract quality metadata
    data_quality = row.get('data_quality', 'COMPLETE')

    return {
        'borrow_rate': row['borrow_rate_close'],
        'availability': row['availability'],
        'borrow_rate_open': row.get('borrow_rate_open'),
        'borrow_rate_high': row.get('borrow_rate_high'),
        'borrow_rate_low': row.get('borrow_rate_low'),

        # Quality metadata
        'data_quality': data_quality,
        'snapshot_count': row.get('snapshot_count'),
        'expected_snapshots': row.get('expected_snapshots'),
        'completeness_ratio': row.get('completeness_ratio'),
    }
```

#### 2.3 Signal Computation - Handle Degraded Data

**Repository:** `flow-state-monitor`
**File:** `src/flow_state_monitor/cli.py`

For DEGRADED days, skip unreliable intraday features:
```python
def _compute_borrow_signals(borrow_data, ...):
    """Compute borrow signals, handling degraded data."""

    quality = borrow_data.get('data_quality', 'COMPLETE')

    # Core signals (always compute from EOD close)
    signals = {
        'borrow_delta': compute_delta(borrow_data['borrow_rate']),
        'borrow_momentum': compute_momentum(...),
        'borrow_level': compute_level(...),
    }

    # Intraday features - only for complete data
    if quality == 'COMPLETE':
        signals.update({
            'intraday_volatility': compute_volatility(borrow_data),
            'intraday_trend': compute_trend(borrow_data),
            'availability_changes': borrow_data.get('availability_changes', 0),
        })

    # Add quality metadata
    signals['meta'] = {
        'data_quality': quality,
        'snapshot_count': borrow_data.get('snapshot_count'),
        'completeness_ratio': borrow_data.get('completeness_ratio'),
    }

    if quality == 'DEGRADED':
        signals['meta']['note'] = 'Intraday features unavailable'

    return signals
```

---

### Phase 3: Dashboard - Minimal Awareness

**Repository:** `market-flow-dashboard`
**File:** `dashboard.py`

#### 3.1 Timeline Indicator (Optional)
Add subtle indicator for degraded days:
```python
def get_timeline_symbol_and_color(record):
    """Get display symbol and color, with quality indicator."""
    # ... existing logic ...

    # Check for degraded quality
    degraded = False
    if flow_state and flow_state.get('signals'):
        meta = flow_state['signals'].get('meta', {})
        if meta.get('data_quality') == 'DEGRADED':
            degraded = True

    # Add asterisk or mark somehow
    if degraded and char:
        char = char + '*'  # e.g., "○*"

    return char, color
```

#### 3.2 Statistics Update
In "Data Quality" section:
```python
def render_data_quality_section(recorder):
    """Show data quality statistics."""
    st.subheader("Data Quality")

    # ... existing stats ...

    # Count degraded days
    degraded_count = count_degraded_days(recorder)
    if degraded_count > 0:
        st.warning(f"{degraded_count} days with degraded borrow data (50-90% snapshots)")
    else:
        st.success("All borrow data complete")
```

---

### Phase 4: Documentation

#### 4.1 Known Gaps Registry

**Repository:** `ibkr-borrow-collector`
**File:** `docs/KNOWN_GAPS.md` (new)

```markdown
# Known Data Gaps

## 2026-02-02 GitHub/Azure Outage
- **Duration:** 2026-02-02 14:00 UTC - 16:00 UTC (2 hours)
- **Impact:** 8 missed collection runs (every 15 min)
- **Affected Markets:** All (USA, Germany, UK, etc.)
- **Affected Symbols:** ~15,000 global symbols
- **Data Quality:** DEGRADED (78/96 snapshots, 81% complete)
- **Root Cause:** Azure cloud outage affecting GitHub Actions
- **Handling:** Fall back to iBorrowDesk historic EOD data where available
- **Flow States:** Created with reduced signal set (no intraday features)

## Template for Future Gaps
- **Duration:**
- **Impact:**
- **Data Quality:**
- **Root Cause:**
- **Handling:**
```

---

## Summary Table

### Quality Thresholds by Data Source

| Data Source | Expected | SEVERE (skip) | DEGRADED (caution) | MINOR (complete) |
|-------------|----------|---------------|-------------------|------------------|
| IBKR Intraday | 96/day | < 48 (< 50%) | 48-86 (50-90%) | 87-96 (90-100%) |
| iBorrowDesk Historic | 1/day | 0 (missing) | N/A | 1 (present) |
| iBorrowDesk Realtime | 1+/day | 0 (missing) | N/A | 1+ (present) |

### 2026-02-02 Outage Handling

**Data:** 78/96 snapshots = 81% = **DEGRADED**

**Actions:**
1. ✅ Parquet cache marks as DEGRADED
2. ✅ Multi provider tries iBorrowDesk historic first (if available)
3. ✅ Signal computation uses EOD close for momentum
4. ❌ Skip intraday features (volatility, trend, availability changes)
5. ✅ Create flow_state with reduced confidence
6. ✅ Dashboard shows normally, optional `*` indicator

**Result:** Better than skipping entire day, prevents false intraday signals.

---

## Benefits

✅ **Prevents false signals** - SEVERE gaps skipped entirely
✅ **Intelligent fallback** - Prefers reliable single EOD over incomplete intraday
✅ **Layered response** - Different handling for different severity levels
✅ **Historic data aware** - Respects 1 snapshot/day data sources
✅ **Minimal dashboard impact** - No elaborate visualizations needed
✅ **Future-proof** - Framework handles any future outages

---

## Cross-Repository Impact

This plan requires changes across multiple repositories:

### ibkr-borrow-collector
- [ ] Add quality assessment to parquet cache builder
- [ ] Store quality metadata in daily aggregates
- [ ] Create KNOWN_GAPS.md documentation

### flow-state-monitor
- [ ] Update IBKR provider to pass quality metadata
- [ ] Implement smart fallback in multi borrow provider
- [ ] Handle degraded data in signal computation
- [ ] Skip intraday features for degraded days

### market-flow-dashboard
- [ ] Add optional quality indicator to timeline
- [ ] Update statistics section to show degraded day count
- [ ] Minimal user-facing changes

---

## Implementation Timeline

**Week 1:**
- [ ] Implement quality assessment in parquet cache
- [ ] Store quality metadata in daily aggregates
- [ ] Create KNOWN_GAPS.md and document 2026-02-02

**Week 2:**
- [ ] Update IBKR provider to pass quality through
- [ ] Implement smart fallback in multi provider
- [ ] Handle degraded data in signal computation

**Week 3:**
- [ ] Add dashboard quality indicator (optional)
- [ ] Update statistics section
- [ ] Test with 2026-02-02 data

**Week 4:**
- [ ] Rebuild parquet cache with quality flags
- [ ] Re-process 2026-02-02 with new logic
- [ ] Verify flow_states created with correct handling
