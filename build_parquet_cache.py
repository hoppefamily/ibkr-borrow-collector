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
import sys
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

        # List all CSV.gz files for the date
        date_str = target_date.strftime("%Y-%m-%d")
        date_prefix = f"{self._source_prefix}/{date_str}/"

        try:
            response = self._s3.list_objects_v2(
                Bucket=self._bucket,
                Prefix=date_prefix
            )
        except Exception as e:
            logger.error(f"Failed to list snapshots for {date_str}: {e}")
            return False

        if "Contents" not in response:
            logger.warning(f"No snapshots found for {date_str}")
            return False

        # Debug logging
        total_files = len(response["Contents"])
        logger.info(f"Found {total_files} total files for {date_str}")
        if total_files > 0:
            logger.debug(f"  Sample keys: {[obj['Key'] for obj in response['Contents'][:3]]}")

        # Filter for baseline snapshots (*.txt.gz files for the market)
        snapshots = [
            obj["Key"]
            for obj in response["Contents"]
            if obj["Key"].endswith(".txt.gz") and f"/{market}-" in obj["Key"]
        ]

        if not snapshots:
            # Debug: show what we DID find
            txt_gz_files = [obj["Key"] for obj in response["Contents"] if obj["Key"].endswith(".txt.gz")]
            logger.warning(f"No {market} snapshots found for {date_str}")
            logger.warning(f"  Found {len(txt_gz_files)} .txt.gz files total: {txt_gz_files[:5] if txt_gz_files else 'none'}")
            return False

        logger.info(f"Found {len(snapshots)} snapshots for {market} {date_str}")

        # Read and combine all snapshots
        all_dfs = []
        for snapshot_key in sorted(snapshots):
            try:
                df = self._read_snapshot(snapshot_key, target_date)
                if df is not None and not df.empty:
                    all_dfs.append(df)
                    logger.debug(f"  ✓ Read {len(df):,} rows from {Path(snapshot_key).name}")
            except Exception as e:
                logger.warning(f"  ✗ Failed to read snapshot {snapshot_key}: {e}")
                continue

        if not all_dfs:
            logger.error(f"Failed to read any snapshots for {date_str}")
            return False

        # Combine all snapshots
        combined_df = pd.concat(all_dfs, ignore_index=True)
        logger.info(
            f"Combined {len(all_dfs)} snapshots: {len(combined_df):,} rows, "
            f"{len(combined_df['symbol'].unique()):,} unique symbols"
        )

        # Deduplicate: keep only rows where borrow_rate or availability changed
        combined_df = combined_df.sort_values(['symbol', 'snapshot_time'])

        deduplicated_df = combined_df.groupby('symbol', group_keys=False).apply(
            lambda group: group[
                (group['borrow_rate_annual'] != group['borrow_rate_annual'].shift()) |
                (group['availability'] != group['availability'].shift())
            ]
        ).reset_index(drop=True)

        rows_before = len(combined_df)
        rows_after = len(deduplicated_df)
        compression_pct = (1 - rows_after / rows_before) * 100 if rows_before > 0 else 0

        logger.info(
            f"Deduplicated: {rows_before:,} → {rows_after:,} rows "
            f"({compression_pct:.1f}% reduction)"
        )

        # Write to Parquet
        self._write_parquet(deduplicated_df, cache_key)

        return True

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
        except Exception as e:
            logger.error(f"Failed to build cache for {target_date}: {e}")
            fail_count += 1

    # Summary
    logger.info("")
    logger.info("=" * 80)
    logger.info("SUMMARY")
    logger.info("=" * 80)
    logger.info(f"Total dates: {len(dates)}")
    logger.info(f"✓ Built: {success_count}")
    logger.info(f"○ Skipped: {skip_count}")
    logger.info(f"✗ Failed: {fail_count}")

    return 0 if fail_count == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
