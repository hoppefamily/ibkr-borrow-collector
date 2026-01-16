#!/usr/bin/env python3
"""
IBKR FTP Data Collector

Downloads borrow and margin files from IBKR FTP, verifies checksums,
compresses, and uploads to S3.

Phase 2: Delta compression with xdelta3 for storage efficiency
"""

import argparse
import ftplib
import gzip
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from typing import Dict, List, Optional, Tuple

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    print("ERROR: boto3 is required. Install with: pip install boto3")
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class FTPDownloader:
    """Handle FTP connection and file downloads."""

    def __init__(self, host: str, user: str, password: str):
        self.host = host
        self.user = user
        self.password = password
        self.ftp: Optional[ftplib.FTP] = None

    def connect(self, timeout: int = 30) -> None:
        """Connect to FTP server."""
        logger.info(f"Connecting to FTP server: {self.host}")
        try:
            self.ftp = ftplib.FTP(self.host, timeout=timeout)
            self.ftp.login(self.user, self.password)
            logger.info(f"Connected to {self.host}")
            logger.info(f"Welcome message: {self.ftp.getwelcome()}")
        except Exception as e:
            logger.error(f"Failed to connect to FTP: {e}")
            raise

    def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from FTP server."""
        if not self.ftp:
            raise RuntimeError("Not connected to FTP server. Call connect() first.")

        try:
            logger.info(f"Downloading {remote_path} -> {local_path}")
            with open(local_path, 'wb') as f:
                self.ftp.retrbinary(f'RETR {remote_path}', f.write)

            size = os.path.getsize(local_path)
            logger.info(f"Downloaded {size:,} bytes")
            return True

        except ftplib.error_perm as e:
            logger.error(f"Permission error downloading {remote_path}: {e}")
            return False
        except Exception as e:
            logger.error(f"Error downloading {remote_path}: {e}")
            raise

    def close(self) -> None:
        """Close FTP connection."""
        if self.ftp:
            try:
                self.ftp.quit()
                logger.info("FTP connection closed")
            except Exception:
                pass


class MD5Verifier:
    """Verify file integrity using MD5 checksums."""

    @staticmethod
    def calculate_md5(filepath: str) -> str:
        """Calculate MD5 hash of a file."""
        md5_hash = hashlib.md5()
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b''):
                md5_hash.update(chunk)
        return md5_hash.hexdigest()

    @staticmethod
    def read_checksum_file(checksum_path: str) -> Optional[str]:
        """Read MD5 checksum from .md5 file."""
        try:
            with open(checksum_path, 'r') as f:
                content = f.read().strip()
                # MD5 files typically have format: "hash  filename" or just "hash"
                parts = content.split()
                return parts[0].lower()
        except Exception as e:
            logger.error(f"Error reading checksum file {checksum_path}: {e}")
            return None

    @staticmethod
    def verify(filepath: str, checksum_path: str, strict: bool = False) -> bool:
        """Verify file against its MD5 checksum.

        Args:
            filepath: Path to file to verify
            checksum_path: Path to .md5 file
            strict: If True, return False on mismatch. If False, only warn.

        Returns:
            True if verified or mismatch in non-strict mode, False only if strict and mismatched
        """
        expected = MD5Verifier.read_checksum_file(checksum_path)
        if not expected:
            logger.warning(f"Could not read expected checksum from {checksum_path}")
            return not strict  # Continue in non-strict mode

        actual = MD5Verifier.calculate_md5(filepath)

        if actual == expected:
            logger.info(f"✓ MD5 verified: {actual}")
            return True
        else:
            if strict:
                logger.error(f"✗ MD5 mismatch! Expected: {expected}, Got: {actual}")
                return False
            else:
                logger.warning("⚠ MD5 mismatch (non-fatal): Server MD5 may be outdated")
                logger.warning(f"  Server: {expected}")
                logger.warning(f"  Actual: {actual}")
                logger.info(f"  Continuing with actual MD5: {actual}")
                return True


class FileCompressor:
    """Compress files with gzip."""

    @staticmethod
    def compress(input_path: str, output_path: Optional[str] = None, level: int = 9) -> str:
        """Compress file with gzip at specified level."""
        if output_path is None:
            output_path = f"{input_path}.gz"

        original_size = os.path.getsize(input_path)
        logger.info(f"Compressing {input_path} (level {level})")

        with open(input_path, 'rb') as f_in:
            with gzip.open(output_path, 'wb', compresslevel=level) as f_out:
                f_out.writelines(f_in)

        compressed_size = os.path.getsize(output_path)
        ratio = (1 - compressed_size / original_size) * 100
        logger.info(f"Compressed: {original_size:,} -> {compressed_size:,} bytes ({ratio:.1f}% reduction)")

        return output_path


class XDeltaCompressor:
    """Create binary diffs using xdelta3."""

    @staticmethod
    def is_available() -> bool:
        """Check if xdelta3 is installed."""
        try:
            subprocess.run(['xdelta3', '-V'], capture_output=True, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False

    @staticmethod
    def create_delta(source_path: str, target_path: str, delta_path: str) -> bool:
        """Create delta from source to target."""
        try:
            logger.info(f"Creating xdelta3 delta: {source_path} -> {target_path}")
            subprocess.run(
                ['xdelta3', '-e', '-9', '-s', source_path, target_path, delta_path],
                capture_output=True,
                check=True
            )
            delta_size = os.path.getsize(delta_path)
            target_size = os.path.getsize(target_path)
            savings = (1 - delta_size / target_size) * 100 if target_size > 0 else 0
            logger.info(f"Delta created: {delta_size:,} bytes ({savings:.1f}% savings)")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"xdelta3 failed: {e.stderr.decode() if e.stderr else str(e)}")
            return False
        except Exception as e:
            logger.error(f"Error creating delta: {e}")
            return False


class S3Uploader:
    """Upload files to S3 with metadata."""

    def __init__(self, bucket: str):
        self.bucket = bucket
        self.s3_client = boto3.client('s3')

    def get_object_metadata(self, s3_key: str) -> Optional[Dict]:
        """Get metadata of an existing S3 object.

        Returns:
            Metadata dict if object exists, None otherwise
        """
        try:
            response = self.s3_client.head_object(Bucket=self.bucket, Key=s3_key)
            return response.get('Metadata', {})
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                return None
            else:
                logger.warning(f"Error checking S3 object {s3_key}: {e}")
                return None

    def list_objects(self, prefix: str) -> List[str]:
        """List objects under a prefix."""
        try:
            response = self.s3_client.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
            if 'Contents' not in response:
                return []
            return [obj['Key'] for obj in response['Contents']]
        except ClientError as e:
            logger.warning(f"Error listing S3 objects under {prefix}: {e}")
            return []

    def download_file(self, s3_key: str, local_path: str, use_cache: bool = True, cache_dir: Optional[str] = None) -> bool:
        """Download file from S3, with optional caching.

        Args:
            s3_key: S3 object key
            local_path: Local destination path
            use_cache: If True and cache_dir provided, check cache first
            cache_dir: Directory for cached files (e.g., ~/.ibkr-baselines)
        """
        # Check cache first if enabled
        if use_cache and cache_dir:
            cache_path = os.path.join(cache_dir, s3_key.replace('/', '_'))
            if os.path.exists(cache_path):
                logger.info(f"Using cached file: {cache_path}")
                import shutil
                shutil.copy2(cache_path, local_path)
                return True

        # Download from S3
        try:
            logger.info(f"Downloading s3://{self.bucket}/{s3_key} -> {local_path}")
            self.s3_client.download_file(self.bucket, s3_key, local_path)
            logger.info("✓ Download successful")

            # Update cache if enabled
            if use_cache and cache_dir:
                os.makedirs(cache_dir, exist_ok=True)
                cache_path = os.path.join(cache_dir, s3_key.replace('/', '_'))
                import shutil
                shutil.copy2(local_path, cache_path)
                logger.debug(f"Cached to {cache_path}")

            return True
        except ClientError as e:
            logger.error(f"S3 download failed: {e}")
            return False

    def upload_file(self, local_path: str, s3_key: str, metadata: Optional[Dict] = None) -> bool:
        """Upload file to S3 with metadata."""
        try:
            extra_args = {}
            if metadata:
                extra_args['Metadata'] = metadata

            logger.info(f"Uploading to s3://{self.bucket}/{s3_key}")
            self.s3_client.upload_file(local_path, self.bucket, s3_key, ExtraArgs=extra_args)
            logger.info("✓ Upload successful")
            return True

        except ClientError as e:
            logger.error(f"S3 upload failed: {e}")
            return False


class CollectionLogger:
    """Track collection statistics and create JSON logs."""

    def __init__(self):
        self.start_time = datetime.utcnow()
        self.files: List[Dict] = []
        self.stats = {
            'processed': 0,
            'uploaded': 0,
            'skipped': 0,
            'failed': 0,
            'total_bytes_downloaded': 0,
            'total_bytes_uploaded': 0
        }

    def log_file(self, filename: str, status: str, **kwargs) -> None:
        """Log file processing result."""
        file_entry = {
            'filename': filename,
            'status': status,
            'timestamp': datetime.utcnow().isoformat()
        }
        file_entry.update(kwargs)
        self.files.append(file_entry)

        self.stats['processed'] += 1
        if status == 'uploaded':
            self.stats['uploaded'] += 1
        elif status == 'skipped':
            self.stats['skipped'] += 1
        elif status == 'failed':
            self.stats['failed'] += 1

    def write_json_log(self, output_file: Optional[str] = None) -> str:
        """Write structured JSON log."""
        end_time = datetime.utcnow()
        duration = (end_time - self.start_time).total_seconds()

        log_data = {
            'run_id': self.start_time.strftime('%Y%m%d_%H%M%S'),
            'start_time': self.start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'duration_seconds': duration,
            'statistics': self.stats,
            'files': self.files
        }

        if output_file:
            with open(output_file, 'w') as f:
                json.dump(log_data, f, indent=2)
            logger.info(f"Log written to {output_file}")

        return json.dumps(log_data, indent=2)


def find_latest_snapshot(s3: S3Uploader, s3_prefix: str, filename_base: str) -> Optional[Tuple[str, str, str]]:
    """Find the most recent snapshot (baseline or delta) for chained delta compression.

    Returns:
        Tuple of (s3_key, timestamp, snapshot_type) or None if no snapshot found
        snapshot_type is 'baseline' or 'delta'
    """
    # List all files for this market in today's folder
    all_files = s3.list_objects(s3_prefix)

    # Find all snapshots (baselines and deltas)
    snapshots = []
    for s3_key in all_files:
        if filename_base in s3_key:
            # Extract timestamp from filename like usa-20260116_120530.txt.gz or usa-20260116_120530.xdelta
            try:
                parts = s3_key.split('/')[-1].split('-')
                if len(parts) >= 2:
                    if s3_key.endswith('.gz') and '.xdelta' not in s3_key:
                        # Baseline
                        timestamp = parts[1].split('.')[0]
                        snapshots.append((s3_key, timestamp, 'baseline'))
                    elif s3_key.endswith('.xdelta'):
                        # Delta
                        timestamp = parts[1].split('.')[0]
                        snapshots.append((s3_key, timestamp, 'delta'))
            except Exception:
                continue

    if not snapshots:
        return None

    # Return the most recent snapshot (baseline or delta)
    snapshots.sort(key=lambda x: x[1], reverse=True)
    return snapshots[0]


def find_latest_baseline(s3: S3Uploader, s3_prefix: str, filename_base: str) -> Optional[Tuple[str, str]]:
    """Find the most recent baseline file (for fallback reconstruction).

    Returns:
        Tuple of (s3_key, timestamp) or None if no baseline found
    """
    # List all files for this market in today's folder
    all_files = s3.list_objects(s3_prefix)

    # Find baseline files only
    baselines = []
    for s3_key in all_files:
        if filename_base in s3_key and s3_key.endswith('.gz') and '.xdelta' not in s3_key:
            try:
                parts = s3_key.split('/')[-1].split('-')
                if len(parts) >= 2:
                    timestamp = parts[1].split('.')[0]
                    baselines.append((s3_key, timestamp))
            except Exception:
                continue

    if not baselines:
        return None

    baselines.sort(key=lambda x: x[1], reverse=True)
    return baselines[0]


def should_create_baseline(current_time: datetime) -> bool:
    """Determine if we should create a new baseline.

    Creates baseline at the start of each hour (00-09 minutes).
    """
    return current_time.minute < 10


def process_file(
    ftp: FTPDownloader,
    s3: S3Uploader,
    collector_logger: CollectionLogger,
    filename: str,
    file_type: str,
    s3_prefix: str,
    temp_dir: str,
    use_delta: bool = True,
    force_upload: bool = False,
    cache_dir: Optional[str] = None
) -> bool:
    """Process a single file: download, verify, and upload with delta compression."""

    checksum_filename = f"{filename}.md5"
    local_file = os.path.join(temp_dir, filename)
    local_checksum = os.path.join(temp_dir, checksum_filename)

    # Download file
    if not ftp.download_file(filename, local_file):
        collector_logger.log_file(filename, 'failed', reason='download_failed')
        return False

    # Download checksum
    if not ftp.download_file(checksum_filename, local_checksum):
        logger.warning(f"Checksum file not found for {filename}, skipping verification")
    else:
        MD5Verifier.verify(local_file, local_checksum, strict=False)

    # Calculate file stats
    original_size = os.path.getsize(local_file)
    content_md5 = MD5Verifier.calculate_md5(local_file)
    collector_logger.stats['total_bytes_downloaded'] += original_size

    # Generate timestamped filename
    current_time = datetime.utcnow()
    timestamp = current_time.strftime('%Y%m%d_%H%M%S')

    # Handle different file extensions (.txt or .dat)
    if filename.endswith('.txt'):
        base_name = filename.rsplit('.', 1)[0]  # Remove .txt extension
        file_extension = 'txt'
    elif filename.endswith('.dat'):
        base_name = filename.rsplit('.', 1)[0]  # Remove .dat extension
        file_extension = 'dat'
    else:
        base_name = filename
        file_extension = 'txt'

    # Decide whether to create baseline or delta
    xdelta_available = XDeltaCompressor.is_available()
    is_scheduled_baseline = should_create_baseline(current_time)
    create_baseline = is_scheduled_baseline or not use_delta or not xdelta_available

    # Warn if xdelta3 is not available
    if not xdelta_available:
        logger.warning("⚠️  xdelta3 is not available - falling back to baseline creation")

    if create_baseline:
        # Create full baseline snapshot
        reason = "scheduled baseline" if is_scheduled_baseline else "delta unavailable" if not xdelta_available else "delta disabled"
        logger.info(f"Creating baseline snapshot for {filename} (reason: {reason})")
        timestamped_name = f"{base_name}-{timestamp}.{file_extension}.gz"
        s3_key = f"{s3_prefix}/{timestamped_name}"

        # Compress
        compressed_file = FileCompressor.compress(local_file)
        compressed_size = os.path.getsize(compressed_file)
        collector_logger.stats['total_bytes_uploaded'] += compressed_size

        # Upload
        metadata = {
            'original-md5': content_md5,
            'file-type': file_type,
            'snapshot-type': 'baseline',
            'collection-time': current_time.isoformat(),
            'original-size': str(original_size),
            'compressed-size': str(compressed_size)
        }

        if s3.upload_file(compressed_file, s3_key, metadata):
            collector_logger.log_file(
                filename,
                'uploaded',
                snapshot_type='baseline',
                size_original=original_size,
                size_compressed=compressed_size,
                md5=content_md5,
                s3_key=s3_key
            )
            return True
        else:
            collector_logger.log_file(filename, 'failed', reason='s3_upload_failed')
            return False

    else:
        # Create chained delta from most recent snapshot (baseline or delta)
        logger.info(f"Creating chained delta snapshot for {filename}")

        # Find previous snapshot (could be baseline or delta)
        latest_snapshot = find_latest_snapshot(s3, s3_prefix, base_name)
        if not latest_snapshot:
            logger.warning(f"No previous snapshot found for {filename}, creating baseline instead")
            return process_file(ftp, s3, collector_logger, filename, file_type,
                              s3_prefix, temp_dir, use_delta=False, force_upload=force_upload, cache_dir=cache_dir)

        source_s3_key, source_timestamp, source_type = latest_snapshot
        logger.info(f"Using {source_type} as source: {source_s3_key}")

        # Download and reconstruct the source file
        source_local = os.path.join(temp_dir, f"{base_name}_source.{file_extension}")

        if source_type == 'baseline':
            # Download and decompress baseline
            source_local_gz = os.path.join(temp_dir, f"{base_name}_source.{file_extension}.gz")
            if not s3.download_file(source_s3_key, source_local_gz, use_cache=True, cache_dir=cache_dir):
                logger.error("Failed to download source baseline, falling back to full snapshot")
                return process_file(ftp, s3, collector_logger, filename, file_type,
                                  s3_prefix, temp_dir, use_delta=False, force_upload=force_upload, cache_dir=cache_dir)

            with gzip.open(source_local_gz, 'rb') as f_in:
                with open(source_local, 'wb') as f_out:
                    f_out.write(f_in.read())

        else:  # source_type == 'delta'
            # Need to reconstruct from baseline + delta chain
            logger.info("Reconstructing source from delta chain...")

            # Find the baseline for this delta
            latest_baseline = find_latest_baseline(s3, s3_prefix, base_name)
            if not latest_baseline:
                logger.error("No baseline found for delta reconstruction, creating new baseline")
                return process_file(ftp, s3, collector_logger, filename, file_type,
                                  s3_prefix, temp_dir, use_delta=False, force_upload=force_upload, cache_dir=cache_dir)

            baseline_s3_key, _ = latest_baseline
            baseline_local_gz = os.path.join(temp_dir, f"{base_name}_baseline.{file_extension}.gz")
            baseline_local = os.path.join(temp_dir, f"{base_name}_baseline.{file_extension}")

            if not s3.download_file(baseline_s3_key, baseline_local_gz, use_cache=True, cache_dir=cache_dir):
                logger.error("Failed to download baseline for reconstruction")
                return process_file(ftp, s3, collector_logger, filename, file_type,
                                  s3_prefix, temp_dir, use_delta=False, force_upload=force_upload, cache_dir=cache_dir)

            with gzip.open(baseline_local_gz, 'rb') as f_in:
                with open(baseline_local, 'wb') as f_out:
                    f_out.write(f_in.read())

            # Download the source delta
            source_delta_local = os.path.join(temp_dir, f"{base_name}_source.xdelta")
            if not s3.download_file(source_s3_key, source_delta_local, use_cache=False):
                logger.error("Failed to download source delta, falling back to full snapshot")
                return process_file(ftp, s3, collector_logger, filename, file_type,
                                  s3_prefix, temp_dir, use_delta=False, force_upload=force_upload, cache_dir=cache_dir)

            # Apply delta to reconstruct source
            try:
                subprocess.run(
                    ['xdelta3', '-d', '-s', baseline_local, source_delta_local, source_local],
                    capture_output=True,
                    check=True
                )
                logger.info("Source reconstructed successfully")
            except subprocess.CalledProcessError as e:
                logger.error(f"Delta reconstruction failed: {e.stderr.decode() if e.stderr else str(e)}")
                logger.error("Falling back to full snapshot")
                return process_file(ftp, s3, collector_logger, filename, file_type,
                                  s3_prefix, temp_dir, use_delta=False, force_upload=force_upload, cache_dir=cache_dir)

        # Check if content changed
        source_md5 = MD5Verifier.calculate_md5(source_local)
        if source_md5 == content_md5 and not force_upload:
            logger.info(f"⊘ File unchanged (MD5: {content_md5}), skipping upload")
            collector_logger.log_file(
                filename,
                'skipped',
                size_original=original_size,
                md5=content_md5,
                reason='unchanged'
            )
            return True

        # Create delta from source to current
        delta_file = os.path.join(temp_dir, f"{base_name}-{timestamp}.xdelta")
        if not XDeltaCompressor.create_delta(source_local, local_file, delta_file):
            logger.error("Delta creation failed, falling back to full snapshot")
            return process_file(ftp, s3, collector_logger, filename, file_type,
                              s3_prefix, temp_dir, use_delta=False, force_upload=force_upload, cache_dir=cache_dir)

        delta_size = os.path.getsize(delta_file)
        collector_logger.stats['total_bytes_uploaded'] += delta_size

        # Upload delta with chain metadata
        delta_s3_key = f"{s3_prefix}/{base_name}-{timestamp}.xdelta"
        metadata = {
            'original-md5': content_md5,
            'source-md5': source_md5,
            'source-key': source_s3_key,
            'source-type': source_type,
            'file-type': file_type,
            'snapshot-type': 'delta',
            'collection-time': current_time.isoformat(),
            'original-size': str(original_size),
            'delta-size': str(delta_size)
        }

        if s3.upload_file(delta_file, delta_s3_key, metadata):
            collector_logger.log_file(
                filename,
                'uploaded',
                snapshot_type='delta',
                source_type=source_type,
                size_original=original_size,
                size_delta=delta_size,
                md5=content_md5,
                source_md5=source_md5,
                s3_key=delta_s3_key
            )
            return True
        else:
            collector_logger.log_file(filename, 'failed', reason='s3_upload_failed')
            return False


def main():
    parser = argparse.ArgumentParser(description='IBKR FTP Data Collector')
    parser.add_argument('--ftp-host', default='ftp2.interactivebrokers.com',
                        help='FTP server hostname')
    parser.add_argument('--ftp-user', default=os.getenv('FTP_USER', 'shortstock'),
                        help='FTP username (default: shortstock for public access)')
    parser.add_argument('--ftp-pass', default=os.getenv('FTP_PASS', ''),
                        help='FTP password (default: empty for public access)')
    parser.add_argument('--s3-bucket', required=True,
                        help='S3 bucket name')
    parser.add_argument('--s3-prefix', default='ibkr/borrow',
                        help='S3 prefix for borrow files')
    parser.add_argument('--dry-run', action='store_true',
                        help='Download but do not upload to S3')
    parser.add_argument('--test-connection', action='store_true',
                        help='Test FTP connection only')
    parser.add_argument('--log-json', action='store_true',
                        help='Output structured JSON log')
    parser.add_argument('--cache-dir', default=os.path.expanduser('~/.ibkr-baselines'),
                        help='Directory for caching baseline snapshots')

    args = parser.parse_args()

    # Initialize
    date_str = datetime.utcnow().strftime('%Y-%m-%d')
    collector_logger = CollectionLogger()

    # Setup cache directory
    cache_dir = args.cache_dir if not args.dry_run else None

    # Borrow rate files to collect (.txt files)
    borrow_files = [
        'usa.txt',
        'british.txt',
        'germany.txt',
        'swiss.txt',
        'italy.txt',
        'japan.txt',
        'hongkong.txt',
        'australia.txt',
        'austria.txt',
        'belgium.txt',
        'canada.txt',
        'dutch.txt',
        'france.txt',
        'mexico.txt',
        'spain.txt',
        'swedish.txt',
        'singapore.txt',
        'india.txt'
    ]

    # Margin requirement files to collect (.dat files)
    margin_files = [
        'stockmargin_final_dtls.IBLLC-US.dat',
        'stockmargin_final_dtls.IB-UKL.dat',
        'stockmargin_final_dtls.IB-AU.dat',
        'stockmargin_final_dtls.IB-CAN.dat',
        'stockmargin_final_dtls.IB-HK.dat',
        'stockmargin_final_dtls.IB-JP.dat',
        'stockmargin_final_dtls.IB-SG.dat',
        'stockmargin_final_dtls.IB-IN.dat'
    ]

    temp_dir = tempfile.mkdtemp(prefix='ibkr_ftp_')
    logger.info(f"Temporary directory: {temp_dir}")

    try:
        # Connect to FTP
        ftp = FTPDownloader(args.ftp_host, args.ftp_user, args.ftp_pass)
        ftp.connect()

        if args.test_connection:
            logger.info("✓ FTP connection test successful")
            return 0

        # Initialize S3 uploader (unless dry run)
        s3 = None if args.dry_run else S3Uploader(args.s3_bucket)

        # Process borrow rate files (.txt)
        for filename in borrow_files:
            logger.info(f"\n{'='*60}")
            logger.info(f"Processing borrow rates: {filename}")
            logger.info(f"{'='*60}")

            if args.dry_run:
                # Just download and verify
                local_file = os.path.join(temp_dir, filename)
                local_checksum = os.path.join(temp_dir, f"{filename}.md5")

                if ftp.download_file(filename, local_file):
                    if ftp.download_file(f"{filename}.md5", local_checksum):
                        MD5Verifier.verify(local_file, local_checksum)
                    logger.info("✓ Dry run - skipping upload")
            else:
                s3_prefix = f"{args.s3_prefix}/{date_str}"
                process_file(
                    ftp, s3, collector_logger,
                    filename, 'borrow', s3_prefix, temp_dir,
                    cache_dir=cache_dir
                )

        # Process margin requirement files (.dat)
        for filename in margin_files:
            logger.info(f"\n{'='*60}")
            logger.info(f"Processing margin requirements: {filename}")
            logger.info(f"{'='*60}")

            if args.dry_run:
                # Just download and verify
                local_file = os.path.join(temp_dir, filename)
                local_checksum = os.path.join(temp_dir, f"{filename}.md5")

                if ftp.download_file(filename, local_file):
                    if ftp.download_file(f"{filename}.md5", local_checksum):
                        MD5Verifier.verify(local_file, local_checksum)
                    logger.info("✓ Dry run - skipping upload")
            else:
                s3_prefix = f"{args.s3_prefix}/{date_str}"
                process_file(
                    ftp, s3, collector_logger,
                    filename, 'margin', s3_prefix, temp_dir,
                    cache_dir=cache_dir
                )

        # Close FTP
        ftp.close()

        # Check if xdelta3 was available during this run
        xdelta_status = "✓ Available" if XDeltaCompressor.is_available() else "✗ Not Available"

        # Write log
        if args.log_json:
            log_data = json.loads(collector_logger.write_json_log())
            log_data['xdelta3_available'] = XDeltaCompressor.is_available()
            print(json.dumps(log_data))
        else:
            logger.info(f"\n{'='*60}")
            logger.info("Collection Summary")
            logger.info(f"{'='*60}")
            logger.info(f"Processed: {collector_logger.stats['processed']}")
            logger.info(f"Uploaded: {collector_logger.stats['uploaded']}")
            logger.info(f"Failed: {collector_logger.stats['failed']}")
            logger.info(f"Downloaded: {collector_logger.stats['total_bytes_downloaded']:,} bytes")
            logger.info(f"Uploaded: {collector_logger.stats['total_bytes_uploaded']:,} bytes")
            logger.info(f"Delta Compression: {xdelta_status}")

        return 0 if collector_logger.stats['failed'] == 0 else 1

    except Exception as e:
        logger.error(f"Collection failed: {e}", exc_info=True)
        return 1

    finally:
        # Cleanup temp directory
        import shutil
        try:
            shutil.rmtree(temp_dir)
            logger.info(f"Cleaned up temp directory: {temp_dir}")
        except Exception as e:
            logger.warning(f"Failed to clean up temp directory: {e}")


if __name__ == '__main__':
    sys.exit(main())
