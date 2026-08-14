"""Tests for the pure helpers in collector.py.

These cover the parsing and verification logic that decides whether a
downloaded IBKR file is usable. The FTP, S3 and xdelta paths need real
services and are left out.
"""

import hashlib
from datetime import datetime, timezone

import pytest

from collector import FileValidator, MD5Verifier, should_create_baseline


def _write(tmp_path, name, content, newline="\n"):
    path = tmp_path / name
    path.write_bytes(content.replace("\n", newline).encode("utf-8"))
    return str(path)


class TestFileValidatorHasData:
    def test_reads_row_count_from_eof_marker(self, tmp_path):
        path = _write(
            tmp_path,
            "full.txt",
            "#BOF|20260814|10:00\n#SYM|CUR|NAME\nAAPL|USD|Apple\n#EOF|1\n",
        )
        assert FileValidator.has_data(path) is True

    def test_zero_count_in_eof_marker_means_no_data(self, tmp_path):
        """A header-only file still has rows on disk, so the count decides."""
        path = _write(
            tmp_path,
            "empty.txt",
            "#BOF|20260814|10:00\n#SYM|CUR|NAME\n#EOF|0\n",
        )
        assert FileValidator.has_data(path) is False

    def test_falls_back_to_counting_non_comment_lines(self, tmp_path):
        path = _write(tmp_path, "no_eof.txt", "#BOF|20260814|10:00\nAAPL|USD|Apple\n")
        assert FileValidator.has_data(path) is True

    def test_fallback_reports_no_data_when_only_comments(self, tmp_path):
        path = _write(tmp_path, "comments.txt", "#BOF|20260814|10:00\n#SYM|CUR|NAME\n")
        assert FileValidator.has_data(path) is False

    def test_unreadable_file_is_assumed_to_have_data(self, tmp_path):
        """Errs on the side of keeping a file rather than silently dropping it."""
        assert FileValidator.has_data(str(tmp_path / "missing.txt")) is True


class TestMD5Verifier:
    def test_crlf_is_normalized_before_hashing(self, tmp_path):
        """IBKR hashes LF content, so CRLF files must produce the same digest."""
        body = "#BOF|20260814|10:00\nAAPL|USD|Apple\n#EOF|1\n"
        lf = _write(tmp_path, "lf.txt", body)
        crlf = _write(tmp_path, "crlf.txt", body, newline="\r\n")

        assert MD5Verifier.calculate_md5(lf) == MD5Verifier.calculate_md5(crlf)

    def test_digest_matches_hashlib_on_lf_content(self, tmp_path):
        body = "AAPL|USD|Apple\n"
        path = _write(tmp_path, "data.txt", body)
        expected = hashlib.md5(body.encode("utf-8")).hexdigest()

        assert MD5Verifier.calculate_md5(path) == expected

    @pytest.mark.parametrize(
        "content", ["d41d8cd98f00b204e9800998ecf8427e  data.txt", "d41d8cd98f00b204e9800998ecf8427e"]
    )
    def test_reads_checksum_with_and_without_filename(self, tmp_path, content):
        path = _write(tmp_path, "data.md5", content)
        assert MD5Verifier.read_checksum_file(path) == "d41d8cd98f00b204e9800998ecf8427e"

    def test_checksum_is_lowercased(self, tmp_path):
        path = _write(tmp_path, "upper.md5", "D41D8CD98F00B204E9800998ECF8427E  data.txt")
        assert MD5Verifier.read_checksum_file(path) == "d41d8cd98f00b204e9800998ecf8427e"

    def test_missing_checksum_file_returns_none(self, tmp_path):
        assert MD5Verifier.read_checksum_file(str(tmp_path / "absent.md5")) is None

    def test_verify_accepts_matching_checksum(self, tmp_path):
        body = "AAPL|USD|Apple\n"
        data = _write(tmp_path, "data.txt", body)
        digest = hashlib.md5(body.encode("utf-8")).hexdigest()
        checksum = _write(tmp_path, "data.md5", f"{digest}  data.txt")

        assert MD5Verifier.verify(data, checksum, strict=True) is True

    def test_mismatch_only_fails_in_strict_mode(self, tmp_path):
        data = _write(tmp_path, "data.txt", "AAPL|USD|Apple\n")
        checksum = _write(tmp_path, "data.md5", "0" * 32)

        assert MD5Verifier.verify(data, checksum, strict=True) is False
        assert MD5Verifier.verify(data, checksum, strict=False) is True

    def test_unreadable_checksum_only_fails_in_strict_mode(self, tmp_path):
        data = _write(tmp_path, "data.txt", "AAPL|USD|Apple\n")
        absent = str(tmp_path / "absent.md5")

        assert MD5Verifier.verify(data, absent, strict=True) is False
        assert MD5Verifier.verify(data, absent, strict=False) is True


class TestShouldCreateBaseline:
    @pytest.mark.parametrize("minute", [0, 5, 9])
    def test_baseline_within_first_ten_minutes(self, minute):
        assert should_create_baseline(datetime(2026, 8, 14, 10, minute, tzinfo=timezone.utc)) is True

    @pytest.mark.parametrize("minute", [10, 30, 59])
    def test_no_baseline_later_in_the_hour(self, minute):
        assert should_create_baseline(datetime(2026, 8, 14, 10, minute, tzinfo=timezone.utc)) is False
