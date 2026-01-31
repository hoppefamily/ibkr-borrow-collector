#!/usr/bin/env python3
"""
Build Parquet cache from IBKR CSV.gz snapshots.

Converts daily CSV.gz snapshots into consolidated Parquet files for faster analytics.

Features:
- 5-10x better compression than gzip
- Columnar storage for fast filtering
- Single file per day vs 96+ snapshots
- Deduplication (only keep changes)

Usage:
    # Build cache for specific date
    python build_parquet_cache.py --date 2026-01-18

    # Build cache for date range
    python build_parquet_cache.py --start 2026-01-01 --end 2026-01-31

    # Rebuild existing caches
    python build_parquet_cache.py --date 2026-01-18 --force

    # Dry run (show what would be done)
    python build_parquet_cache.py --date 2026-01-18 --dry-run
"""

import argparse
import gzip
import logging
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import List, Optional

import boto3
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class ParquetCacheBuilder:
    """Build Parquet cache from IBKR CSV.gz snapshots."""

    def __init__(
        self,
        s3_client,
        bucket: str,
        source_prefix: str = "ibkr/borrow",
        cache_prefix: str = "ibkr/parquet",
    ):
        self._s3 = s3_client
        self._bucket = bucket
        self._source_prefix = source_prefix.rstrip("/")
        self._cache_prefix = cache_prefix.rstrip("/")

    def get_cache_key(self, market: str, target_date: date) -> str:
        """Get S3 key for cached Parquet file."""
        return f"{self._cache_prefix}/{market}/{target_date.strftime('%Y-%m-%d')}.parquet"

    def cache_exists(self, market: str, target_date: date) -> bool:
        """Check if Parquet cache exists for date."""
        cache_key = self.get_cache_key(market, target_date)
        try:
            self._s3.head_object(Bucket=self._bucket, Key=cache_key)
            return True
        except self._s3.exceptions.ClientError:
            return False

    def _is_xdelta_available(self) -> bool:
        """Check if xdelta3 is installed."""
        try:
            subprocess.run(['xdelta3', '-V'], capture_output=True, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False

    def _reconstruct_from_delta(
        self,
        delta_key: str,
        baseline_key: str,
        temp_dir: str
    ) -> Optional[str]:
        """
        Reconstruct a snapshot from a delta file.

        Args:
            delta_key: S3 key of the delta file
            baseline_key: S3 key of the baseline file
            temp_dir: Temporary directory for processing

        Returns:
            Path to reconstructed file, or None if failed
        """
        # Validate baseline_key points to a gzipped file
        if not baseline_key.endswith('.txt.gz') and not baseline_key.endswith('.dat.gz'):
            logger.error(f"CORRUPT METADATA: Delta {delta_key} has invalid baseline key: {baseline_key}")
            logger.error(f"Expected .txt.gz or .dat.gz file, but got: {baseline_key}")
            raise ValueError(f"Corrupt metadata in delta file {delta_key}: baseline_key={baseline_key}")

        try:
            # Download baseline
            baseline_gz_path = f"{temp_dir}/baseline.txt.gz"
            baseline_path = f"{temp_dir}/baseline.txt"

            logger.debug(f"Downloading baseline: {baseline_key}")
            self._s3.download_file(
                Bucket=self._bucket,
                Key=baseline_key,
                Filename=baseline_gz_path
            )

            # Decompress baseline
            with gzip.open(baseline_gz_path, 'rb') as f_in:
                with open(baseline_path, 'wb') as f_out:
                    f_out.write(f_in.read())

            # Download delta
            delta_path = f"{temp_dir}/delta.xdelta"
            logger.debug(f"Downloading delta: {delta_key}")
            self._s3.download_file(
                Bucket=self._bucket,
                Key=delta_key,
                Filename=delta_path
            )

# Apply delta to reconstruct (use unique filename)
            import uuid
            output_path = f"{temp_dir}/reconstructed_{uuid.uuid4().hex}.txt"
            subprocess.run(
                ['xdelta3', '-d', '-s', baseline_path, delta_path, output_path],
                capture_output=True,
                check=True
            )

            return output_path

        except subprocess.CalledProcessError as e:
            logger.error(f"xdelta3 reconstruction failed for {delta_key}: {e.stderr.decode() if e.stderr else str(e)}")
            return None
        except Exception as e:
            logger.error(f"Failed to reconstruct delta {delta_key}: {e}")
            return None

    def _find_baseline_for_delta(self, delta_key: str) -> Optional[str]:
        """
        Find the baseline that a delta is based on using S3 metadata.

        Args:
            delta_key: S3 key of the delta file

        Returns:
            S3 key of the baseline, or None if not found
        """
        try:
            response = self._s3.head_object(Bucket=self._bucket, Key=delta_key)
            metadata = response.get('Metadata', {})
            source_key = metadata.get('source-key')

            if source_key:
                logger.debug(f"Delta {Path(delta_key).name} -> baseline {Path(source_key).name}")
                return source_key
            else:
                logger.warning(f"Delta {delta_key} missing source-key metadata")
                return None

        except Exception as e:
            logger.error(f"Failed to get metadata for {delta_key}: {e}")
            return None

    def _compute_daily_aggregates(self, df: pd.DataFrame, target_date: date) -> pd.DataFrame:
        """
        Compute daily aggregate features from intraday snapshots.

        Generates one row per symbol with OHLC-style borrow rate aggregates and intraday metrics.

        Args:
            df: DataFrame with all intraday snapshots (symbol, timestamp, borrow_rate_annual,
                availability, snapshot_time)
            target_date: The date for the aggregates

        Returns:
            DataFrame with daily aggregate rows containing:
                - borrow_rate_open: First snapshot of day
                - borrow_rate_close: Last snapshot of day
                - borrow_rate_high: Maximum during day
                - borrow_rate_low: Minimum during day
                - borrow_rate_mean: Time-weighted average
                - borrow_rate_std: Standard deviation (volatility measure)
                - intraday_trend: Close - Open (directional pressure)
                - availability_changes: Count of availability state transitions
        """
        aggregates = []

        for symbol in df['symbol'].unique():
            symbol_df = df[df['symbol'] == symbol].sort_values('snapshot_time')

            if len(symbol_df) == 0:
                continue

            rates = symbol_df['borrow_rate_annual']
            avail = symbol_df['availability']

            # OHLC aggregates
            borrow_rate_open = rates.iloc[0]
            borrow_rate_close = rates.iloc[-1]
            borrow_rate_high = rates.max()
            borrow_rate_low = rates.min()
            borrow_rate_mean = rates.mean()
            borrow_rate_std = rates.std() if len(rates) > 1 else 0.0

            # Intraday trend (close vs open)
            intraday_trend = borrow_rate_close - borrow_rate_open

            # Count availability changes (transitions between states)
            availability_changes = (avail != avail.shift()).sum() - 1  # -1 to exclude first

            # Use close values for main fields (most predictive for next day)
            # but keep the aggregate row marked as aggregate type
            aggregates.append({
                'symbol': symbol,
                'timestamp': target_date,
                'borrow_rate_annual': borrow_rate_close,  # Close is primary
                'availability': avail.iloc[-1],  # Last known availability
                'snapshot_time': symbol_df['snapshot_time'].iloc[-1],  # Mark as EOD
                # Extended features (daily aggregates)
                'borrow_rate_open': borrow_rate_open,
                'borrow_rate_high': borrow_rate_high,
                'borrow_rate_low': borrow_rate_low,
                'borrow_rate_mean': borrow_rate_mean,
                'borrow_rate_std': borrow_rate_std,
                'intraday_trend': intraday_trend,
                'availability_changes': int(availability_changes),
                'snapshot_count': len(symbol_df),  # How many snapshots during day
                'is_daily_aggregate': True,  # Marker to distinguish from intraday rows
            })

        return pd.DataFrame(aggregates)

    def list_available_dates(self, market: str = "usa") -> List[date]:
        """List all dates with CSV.gz snapshots."""
        try:
            paginator = self._s3.get_paginator('list_objects_v2')
            dates = set()

            for page in paginator.paginate(
                Bucket=self._bucket,
                Prefix=f"{self._source_prefix}/",
                Delimiter='/'
            ):
                # CommonPrefixes are date directories like "ibkr/borrow/2026-01-18/"
                for prefix in page.get('CommonPrefixes', []):
                    prefix_str = prefix['Prefix']
                    # Extract date from path
                    parts = prefix_str.rstrip('/').split('/')
                    if parts:
                        date_str = parts[-1]
                        try:
                            dt = datetime.strptime(date_str, '%Y-%m-%d').date()
                            dates.add(dt)
                        except ValueError:
                            continue

            return sorted(dates)

        except Exception as e:
            logger.error(f"Failed to list available dates: {e}")
            return []

    def build_cache(
        self,
        market: str,
        target_date: date,
        force: bool = False,
        dry_run: bool = False
    ) -> bool:
        """
        Build Parquet cache for a date.

        Args:
            market: Market identifier (e.g., 'usa')
            target_date: Date to build cache for
            force: Rebuild even if cache exists
            dry_run: Don't write, just show what would be done

        Returns:
            True if cache was built/would be built
        """
        cache_key = self.get_cache_key(market, target_date)

        # Check if already cached
        if not force and self.cache_exists(market, target_date):
            logger.info(f"✓ Cache already exists: {cache_key}")
            return False

        if dry_run:
            logger.info(f"[DRY RUN] Would build cache: {cache_key}")
            return True

        logger.info(f"Building Parquet cache for {market} {target_date}")

        # Check if xdelta3 is available for delta reconstruction
        xdelta_available = self._is_xdelta_available()
        if not xdelta_available:
            logger.warning("xdelta3 not available - will only process baseline snapshots")

        # List all files for the date
        date_str = target_date.strftime("%Y-%m-%d")
        date_prefix = f"{self._source_prefix}/{date_str}/"

        try:
            # Use paginator to handle >1000 objects
            paginator = self._s3.get_paginator('list_objects_v2')
            pages = paginator.paginate(
                Bucket=self._bucket,
                Prefix=date_prefix
            )

            # Collect all objects from all pages
            all_objects = []
            for page in pages:
                if "Contents" in page:
                    all_objects.extend(page["Contents"])

        except Exception as e:
            error_msg = str(e)
            # AWS credential expiration is a fatal error - fail immediately
            if "ExpiredToken" in error_msg or "expired" in error_msg.lower():
                logger.error(f"FATAL: AWS credentials expired while listing snapshots for {date_str}")
                raise RuntimeError(f"AWS credentials expired: {e}") from e
            logger.error(f"Failed to list snapshots for {date_str}: {e}")
            return False

        if not all_objects:
            logger.warning(f"No snapshots found for {date_str}")
            return False

        # Debug logging
        total_files = len(all_objects)
        logger.info(f"Found {total_files} total files for {date_str}")

        # Filter for baseline snapshots (*.txt.gz files for the market)
        baselines = [
            obj["Key"]
            for obj in all_objects
            if obj["Key"].endswith(".txt.gz") and f"/{market}-" in obj["Key"]
        ]

        # Filter for delta snapshots (*.xdelta files for the market)
        deltas = [
            obj["Key"]
            for obj in all_objects
            if obj["Key"].endswith(".xdelta") and f"/{market}-" in obj["Key"]
        ] if xdelta_available else []

        if not baselines and not deltas:
            # Debug: show what we DID find
            txt_gz_files = [obj["Key"] for obj in all_objects if obj["Key"].endswith(".txt.gz")]
            xdelta_files = [obj["Key"] for obj in all_objects if obj["Key"].endswith(".xdelta")]
            logger.warning(f"No {market} snapshots found for {date_str}")
            logger.warning(f"  Found {len(txt_gz_files)} .txt.gz files total")
            logger.warning(f"  Found {len(xdelta_files)} .xdelta files total")
            return False

        logger.info(f"Found {len(baselines)} baseline + {len(deltas)} delta snapshots for {market} {date_str}")

        # Read and combine all snapshots (baselines + deltas)
        all_dfs = []

        # Process baselines
        for snapshot_key in sorted(baselines):
            try:
                df = self._read_snapshot(snapshot_key, target_date)
                if df is not None and not df.empty:
                    all_dfs.append(df)
                    logger.debug(f"  ✓ Read {len(df):,} rows from baseline {Path(snapshot_key).name}")
            except Exception as e:
                logger.warning(f"  ✗ Failed to read baseline {snapshot_key}: {e}")
                continue

        # Process deltas (if xdelta3 available)
        if xdelta_available and deltas:
            temp_dir = tempfile.mkdtemp(prefix='parquet_cache_')
            delta_failures = []
            try:
                for delta_key in sorted(deltas):
                    try:
                        # Find the baseline this delta is based on
                        baseline_key = self._find_baseline_for_delta(delta_key)
                        if not baseline_key:
                            logger.warning(f"  ✗ Skipping delta {Path(delta_key).name}: baseline not found")
                            continue

                        # Reconstruct the snapshot from delta
                        reconstructed_path = self._reconstruct_from_delta(
                            delta_key, baseline_key, temp_dir
                        )

                        if reconstructed_path:
                            # Read the reconstructed snapshot
                            df = self._read_snapshot_from_file(reconstructed_path, target_date)
                            if df is not None and not df.empty:
                                all_dfs.append(df)
                                logger.debug(f"  ✓ Read {len(df):,} rows from delta {Path(delta_key).name}")
                        else:
                            logger.warning(f"  ✗ Failed to reconstruct delta {delta_key}")

                    except ValueError as e:
                        # Corrupt metadata - this is fatal
                        if "Corrupt metadata" in str(e):
                            logger.error(f"FATAL: {e}")
                            raise
                        # Other ValueErrors are non-fatal
                        delta_failures.append((delta_key, str(e)))
                        logger.warning(f"  ✗ Failed to process delta {delta_key}: {e}")
                        continue
                    except Exception as e:
                        delta_failures.append((delta_key, str(e)))
                        logger.warning(f"  ✗ Failed to process delta {delta_key}: {e}")
                        continue

            finally:
                # Cleanup temp directory
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
            
            # Report delta processing statistics
            if delta_failures:
                logger.warning(
                    f"Delta processing: {len(deltas) - len(delta_failures)}/{len(deltas)} successful, "
                    f"{len(delta_failures)} failed"
                )

        if not all_dfs:
            logger.error(f"Failed to read any snapshots for {date_str}")
            return False

        # Combine all snapshots
        combined_df = pd.concat(all_dfs, ignore_index=True)
        logger.info(
            f"Combined {len(baselines)} baselines + {len(deltas) if xdelta_available else 0} deltas: "
            f"{len(combined_df):,} rows, {len(combined_df['symbol'].unique()):,} unique symbols"
        )

        # Deduplicate: keep only rows where borrow_rate or availability changed
        combined_df = combined_df.sort_values(['symbol', 'snapshot_time'])

        # Deduplicate per symbol - keep first row per symbol and rows where values changed
        # Create mask for rows that changed from previous row within each symbol group
        changed_mask = (
            (combined_df['borrow_rate_annual'] != combined_df.groupby('symbol')['borrow_rate_annual'].shift()) |
            (combined_df['availability'] != combined_df.groupby('symbol')['availability'].shift())
        )

        deduplicated_df = combined_df[changed_mask].copy()

        rows_before = len(combined_df)
        rows_after = len(deduplicated_df)
        compression_pct = (1 - rows_after / rows_before) * 100 if rows_before > 0 else 0

        logger.info(
            f"Deduplicated: {rows_before:,} → {rows_after:,} rows "
            f"({compression_pct:.1f}% reduction)"
        )

        # Compute daily aggregates per symbol for enhanced features
        logger.info("Computing daily aggregates from intraday snapshots...")
        daily_agg_df = self._compute_daily_aggregates(combined_df, target_date)

        # Combine deduplicated intraday data with daily aggregates
        # Daily aggregates have a special marker to distinguish them
        final_df = pd.concat([deduplicated_df, daily_agg_df], ignore_index=True)

        logger.info(
            f"Added {len(daily_agg_df):,} daily aggregate rows "
            f"({len(daily_agg_df['symbol'].unique()):,} symbols)"
        )

        # Write to Parquet
        self._write_parquet(final_df, cache_key)

        return True

    def _read_snapshot_from_file(self, file_path: str, target_date: date) -> Optional[pd.DataFrame]:
        """Read a snapshot from a local file (used for reconstructed deltas)."""
        try:
            with open(file_path, 'r') as f:
                content = f.read()

            # Parse pipe-delimited format
            lines = content.strip().split("\n")

            if len(lines) < 3:
                return None

            # Extract exact timestamp from #BOF header
            snapshot_time = None
            if lines[0].startswith("#BOF"):
                try:
                    bof_parts = lines[0].split("|")
                    if len(bof_parts) >= 3:
                        date_str = bof_parts[1]
                        time_str = bof_parts[2]
                        datetime_str = f"{date_str} {time_str}"
                        snapshot_time = pd.to_datetime(datetime_str, format="%Y.%m.%d %H:%M:%S", utc=True)
                except Exception as e:
                    raise ValueError(f"Failed to parse #BOF timestamp from {file_path}: {e}") from e

            # No fallback - fail hard if #BOF header is missing or invalid
            if snapshot_time is None:
                raise ValueError(
                    f"Missing or invalid #BOF header in {file_path}. "
                    f"Expected format: #BOF|YYYY.MM.DD|HH:MM:SS"
                )

            records = []
            for line in lines:
                if line.startswith("#") or not line.strip():
                    continue

                parts = line.split("|")
                if len(parts) < 9:
                    continue

                symbol = parts[0].strip()
                fee_rate_str = parts[6].strip()

                if not fee_rate_str or fee_rate_str.upper() == "NA":
                    continue

                try:
                    borrow_rate = float(fee_rate_str)

                    available_str = parts[7].strip()
                    if available_str.upper() == "NA":
                        availability = 0
                    elif available_str.startswith(">"):
                        availability = int(available_str[1:])
                    else:
                        availability = int(float(available_str))
                except (ValueError, IndexError):
                    continue

                records.append({
                    "symbol": symbol,
                    "timestamp": target_date,
                    "borrow_rate_annual": borrow_rate,
                    "availability": availability,
                    "snapshot_time": snapshot_time,
                })

            if not records:
                return None

            return pd.DataFrame(records)

        except Exception as e:
            logger.error(f"Error reading snapshot from {file_path}: {e}")
            raise

    def _read_snapshot(self, s3_key: str, target_date: date) -> Optional[pd.DataFrame]:
        """Read a single CSV.gz snapshot and parse into DataFrame."""
        try:
            # Download and decompress
            response = self._s3.get_object(Bucket=self._bucket, Key=s3_key)
            compressed_data = response["Body"].read()

            with gzip.open(BytesIO(compressed_data), "rt") as f:
                content = f.read()

            # Parse pipe-delimited format
            lines = content.strip().split("\n")

            if len(lines) < 3:
                return None

            # Extract exact timestamp from #BOF header
            snapshot_time = None
            if lines[0].startswith("#BOF"):
                try:
                    bof_parts = lines[0].split("|")
                    if len(bof_parts) >= 3:
                        date_str = bof_parts[1]
                        time_str = bof_parts[2]
                        datetime_str = f"{date_str} {time_str}"
                        snapshot_time = pd.to_datetime(datetime_str, format="%Y.%m.%d %H:%M:%S", utc=True)
                except Exception as e:
                    raise ValueError(f"Failed to parse #BOF timestamp from {s3_key}: {e}") from e

            # No fallback - fail hard if #BOF header is missing or invalid
            if snapshot_time is None:
                raise ValueError(
                    f"Missing or invalid #BOF header in {s3_key}. "
                    f"Expected format: #BOF|YYYY.MM.DD|HH:MM:SS"
                )

            records = []
            for line in lines:
                if line.startswith("#") or not line.strip():
                    continue

                parts = line.split("|")
                if len(parts) < 9:
                    continue

                symbol = parts[0].strip()
                fee_rate_str = parts[6].strip()

                if not fee_rate_str or fee_rate_str.upper() == "NA":
                    continue

                try:
                    borrow_rate = float(fee_rate_str)

                    available_str = parts[7].strip()
                    if available_str.upper() == "NA":
                        availability = 0
                    elif available_str.startswith(">"):
                        availability = int(available_str[1:])
                    else:
                        availability = int(float(available_str))
                except (ValueError, IndexError):
                    continue

                records.append({
                    "symbol": symbol,
                    "timestamp": target_date,
                    "borrow_rate_annual": borrow_rate,
                    "availability": availability,
                    "snapshot_time": snapshot_time,
                })

            if not records:
                return None

            return pd.DataFrame(records)

        except Exception as e:
            logger.error(f"Error reading snapshot {s3_key}: {e}")
            raise

    def _write_parquet(self, df: pd.DataFrame, cache_key: str) -> None:
        """Write DataFrame to Parquet on S3."""
        try:
            buffer = BytesIO()
            df.to_parquet(
                buffer,
                engine="pyarrow",
                compression="snappy",
                index=False,
            )
            buffer.seek(0)

            self._s3.put_object(
                Bucket=self._bucket,
                Key=cache_key,
                Body=buffer.getvalue(),
                ContentType="application/vnd.apache.parquet",
            )

            size_mb = len(buffer.getvalue()) / 1024 / 1024
            logger.info(f"✓ Wrote Parquet cache: {cache_key} ({size_mb:.2f} MB, {len(df):,} rows)")

        except Exception as e:
            logger.error(f"Failed to write Parquet cache {cache_key}: {e}")
            raise


def main():
    parser = argparse.ArgumentParser(
        description='Build Parquet cache from IBKR CSV.gz snapshots',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        '--date',
        type=lambda s: datetime.strptime(s, '%Y-%m-%d').date(),
        help='Single date to build cache for (YYYY-MM-DD)'
    )
    parser.add_argument(
        '--start',
        type=lambda s: datetime.strptime(s, '%Y-%m-%d').date(),
        help='Start date for range (YYYY-MM-DD)'
    )
    parser.add_argument(
        '--end',
        type=lambda s: datetime.strptime(s, '%Y-%m-%d').date(),
        help='End date for range (YYYY-MM-DD)'
    )
    parser.add_argument(
        '--market',
        default='usa',
        help='Market to build cache for (default: usa)'
    )
    parser.add_argument(
        '--bucket',
        help='S3 bucket (auto-detected from CloudFormation if not provided)'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Rebuild cache even if it exists'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without doing it'
    )
    parser.add_argument(
        '--list-dates',
        action='store_true',
        help='List all available dates and exit'
    )

    args = parser.parse_args()

    # Auto-detect bucket from CloudFormation
    if not args.bucket:
        try:
            cf = boto3.client('cloudformation')
            stacks = cf.describe_stacks(StackName='ibkr-borrow-collector')
            for output in stacks['Stacks'][0]['Outputs']:
                if output['OutputKey'] == 'BorrowDataBucket':
                    args.bucket = output['OutputValue']
                    break
        except Exception:
            pass

    if not args.bucket:
        parser.error("Could not auto-detect bucket. Please provide --bucket")

    # Initialize
    s3 = boto3.client('s3')
    builder = ParquetCacheBuilder(s3, args.bucket)

    # List dates mode
    if args.list_dates:
        logger.info(f"Listing available dates in s3://{args.bucket}/{builder._source_prefix}/")
        dates = builder.list_available_dates(args.market)
        if dates:
            logger.info(f"Found {len(dates)} dates:")
            for dt in dates:
                cached = "✓" if builder.cache_exists(args.market, dt) else " "
                logger.info(f"  [{cached}] {dt}")
        else:
            logger.warning("No dates found")
        return 0

    # Determine dates to process
    if args.date:
        dates = [args.date]
    elif args.start and args.end:
        dates = []
        current = args.start
        while current <= args.end:
            dates.append(current)
            current += timedelta(days=1)
    else:
        parser.error("Provide --date or --start/--end or --list-dates")

    # Build caches
    logger.info("=" * 80)
    logger.info("Parquet Cache Builder")
    logger.info("=" * 80)
    logger.info(f"Bucket: {args.bucket}")
    logger.info(f"Market: {args.market}")
    logger.info(f"Dates: {len(dates)} ({dates[0]} to {dates[-1]})")
    logger.info(f"Force: {args.force}")
    logger.info(f"Dry run: {args.dry_run}")
    logger.info("")

    success_count = 0
    skip_count = 0
    fail_count = 0
    failed_dates = []

    for i, target_date in enumerate(dates, 1):
        logger.info(f"[{i}/{len(dates)}] Processing {target_date}")
        try:
            built = builder.build_cache(
                args.market,
                target_date,
                force=args.force,
                dry_run=args.dry_run
            )
            if built:
                success_count += 1
            else:
                skip_count += 1
        except RuntimeError as e:
            # Fatal errors like AWS token expiration - fail immediately
            logger.error(f"FATAL: {e}")
            fail_count += 1
            failed_dates.append(target_date)
            break
        except Exception as e:
            logger.error(f"Failed to build cache for {target_date}: {e}")
            fail_count += 1
            failed_dates.append(target_date)

    # Summary
    logger.info("")
    logger.info("=" * 80)
    logger.info("SUMMARY")
    logger.info("=" * 80)
    logger.info(f"Total dates: {len(dates)}")
    logger.info(f"✓ Built: {success_count}")
    logger.info(f"○ Skipped: {skip_count}")
    logger.info(f"✗ Failed: {fail_count}")
    if failed_dates:
        logger.error(f"Failed dates: {', '.join(str(d) for d in failed_dates)}")

    return 0 if fail_count == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
