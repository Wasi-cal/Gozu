# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from cli.prerequisites.archive import verify_checksum
from cli.prerequisites.trivy import _TRIVY_CHECKSUMS, _TRIVY_VERSION, _os_arch_label


def test_every_pinned_version_has_a_checksum_for_every_supported_platform():
    """Every real platform label this module claims to support must have a real pinned checksum."""
    for label in ("macOS-64bit", "macOS-ARM64", "Linux-64bit", "Linux-ARM64", "windows-64bit"):
        assert label in _TRIVY_CHECKSUMS
        assert len(_TRIVY_CHECKSUMS[label]) == 64  # a real sha256 hex digest, not a placeholder


def test_os_arch_label_darwin_arm64():
    with patch("platform.system", return_value="Darwin"), patch("platform.machine", return_value="arm64"):
        assert _os_arch_label() == "macOS-ARM64"


def test_os_arch_label_linux_x86_64():
    with patch("platform.system", return_value="Linux"), patch("platform.machine", return_value="x86_64"):
        assert _os_arch_label() == "Linux-64bit"


def test_os_arch_label_windows_amd64():
    with patch("platform.system", return_value="Windows"), patch("platform.machine", return_value="AMD64"):
        assert _os_arch_label() == "windows-64bit"


def test_os_arch_label_unsupported_os_raises():
    with patch("platform.system", return_value="Plan9"), pytest.raises(RuntimeError, match="No managed Trivy download"):
        _os_arch_label()


def test_os_arch_label_unsupported_arch_raises():
    with patch("platform.system", return_value="Linux"), patch("platform.machine", return_value="riscv64"):
        with pytest.raises(RuntimeError, match="No managed Trivy download"):
            _os_arch_label()


# --- verify_checksum() (archive.py, but exercised here since trivy.py is its first real caller) ---


def test_verify_checksum_passes_for_matching_hash(tmp_path):
    archive = tmp_path / "fake.tar.gz"
    archive.write_bytes(b"some archive bytes")
    real_sha256 = hashlib.sha256(b"some archive bytes").hexdigest()
    verify_checksum(archive, real_sha256)  # doesn't raise


def test_verify_checksum_raises_for_mismatched_hash(tmp_path):
    archive = tmp_path / "fake.tar.gz"
    archive.write_bytes(b"some archive bytes")
    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        verify_checksum(archive, "0" * 64)


def test_verify_checksum_is_case_insensitive(tmp_path):
    archive = tmp_path / "fake.tar.gz"
    archive.write_bytes(b"some archive bytes")
    real_sha256 = hashlib.sha256(b"some archive bytes").hexdigest()
    verify_checksum(archive, real_sha256.upper())  # doesn't raise
