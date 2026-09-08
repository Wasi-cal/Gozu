# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: cli/prerequisites/archive.py - shared download/extract/verify/checksum helpers

"""Ensuring the trivy CLI is available on the host - see scanner.base.ScannerRequirements.host_dependencies."""

import platform
import shutil
import subprocess
from pathlib import Path

import typer

from cli.prerequisites.archive import download_archive, extract_archive, verify_checksum, verify_runnable
from cli.status import success, waiting
from scripts.paths import GOZU_HOME

TRIVY_DIR = GOZU_HOME / "trivy"

# Pinned to a specific, verified-working release - bump both this AND
# _TRIVY_CHECKSUMS together (from that release's own
# trivy_{version}_checksums.txt, https://github.com/aquasecurity/trivy/releases)
# when a newer version is needed. Checksums are baked in here rather than
# fetched from the checksums.txt file at install time - the whole point of
# verifying a download is to not trust a second unverified network
# response just as much as the first; pinning the expected hash alongside
# the version it belongs to means there's nothing left to trust but this
# source file itself.
_TRIVY_VERSION = "0.74.0"

# os_label-arch_label (Trivy's own release-asset naming, NOT
# archive.py's os_name()/arch_name() generic labels - see _os_arch_label())
# -> that asset's real published SHA256.
_TRIVY_CHECKSUMS = {
    "macOS-64bit": "472816f6888dda689d075c30254d4210b4d1035acf365aa72332f584c2f60485",
    "macOS-ARM64": "1caada5e0e2091909357c7525d3aa76f4b660b13821bc143b190c7483e31cc11",
    "Linux-64bit": "2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a",
    "Linux-ARM64": "b94ce1976bbf3c15b514b605ee88be7c6d94a29be2302847ff01cb794d47aad5",
    "windows-64bit": "94c40e0696e4b907a74b7b2e1438d5d72ebaca83115817407f568a002d520842",
}


def _os_arch_label() -> str:
    """
    Trivy's own release-asset naming convention
    (trivy_{version}_{this}.{ext}) - deliberately NOT archive.py's shared
    os_name()/arch_name() (those return "macosx"/"x64"-style labels for
    SonarQube/Adoptium's own conventions, which Trivy doesn't share) -
    same "each ensure_*() maps its own labels" pattern sonar_scanner.py
    and java.py already use, just done as one combined os-arch string
    here since that's the shape Trivy's own filenames use.
    """
    system = platform.system()
    machine = platform.machine().lower()

    os_part = {"Darwin": "macOS", "Linux": "Linux", "Windows": "windows"}.get(system)
    if os_part is None:
        raise RuntimeError(f"No managed Trivy download available for this OS: {system}")

    arch_part = {"x86_64": "64bit", "amd64": "64bit", "arm64": "ARM64", "aarch64": "ARM64"}.get(machine)
    if arch_part is None:
        raise RuntimeError(f"No managed Trivy download available for this CPU architecture: {machine}")

    return f"{os_part}-{arch_part}"


def _managed_trivy_binary() -> Path | None:
    """The trivy binary inside our own ~/.gozu/trivy/, if we've downloaded one before."""
    binary_name = "trivy.exe" if platform.system() == "Windows" else "trivy"
    candidate = TRIVY_DIR / binary_name
    return candidate if candidate.is_file() else None


def check_trivy() -> bool:
    """True if a working trivy is found either on PATH or in our own ~/.gozu/trivy/."""
    binary = shutil.which("trivy") or _managed_trivy_binary()
    if binary is None:
        return False
    try:
        result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=False)
        return result.returncode == 0
    except OSError:
        return False


def ensure_trivy() -> Path:
    """
    Return a path to a usable `trivy` binary, downloading one if needed -
    same fully-contained-directory approach as ensure_sonar_scanner():
    check PATH/a previous managed download first (check_trivy()); only if
    neither exists, download the official release archive for this host's
    OS/arch into ~/.gozu/trivy/ (no system-wide install), verify its
    checksum BEFORE extracting, and confirm it's runnable afterward.
    """
    if check_trivy():
        existing = shutil.which("trivy") or _managed_trivy_binary()
        if existing is None:
            raise RuntimeError("check_trivy() succeeded but no binary could be located")
        return Path(existing)

    typer.echo("trivy CLI wasn't found on this machine.")
    os_arch = _os_arch_label()
    expected_sha256 = _TRIVY_CHECKSUMS.get(os_arch)
    if expected_sha256 is None:
        raise RuntimeError(f"No pinned checksum for Trivy {_TRIVY_VERSION} on {os_arch} - update cli/prerequisites/trivy.py")

    is_zip = os_arch.startswith("windows")
    ext = "zip" if is_zip else "tar.gz"
    filename = f"trivy_{_TRIVY_VERSION}_{os_arch}.{ext}"
    url = f"https://github.com/aquasecurity/trivy/releases/download/v{_TRIVY_VERSION}/{filename}"

    waiting(f"Downloading trivy {_TRIVY_VERSION} ({os_arch}) into {TRIVY_DIR} ...")
    TRIVY_DIR.mkdir(parents=True, exist_ok=True)
    archive = download_archive(url, suffix=f".{ext}", timeout=180)

    waiting("Verifying checksum ...")
    verify_checksum(archive, expected_sha256)

    extract_archive(archive, TRIVY_DIR, is_zip=is_zip)

    trivy_bin = _managed_trivy_binary()
    if trivy_bin is None:
        raise RuntimeError(f"Downloaded trivy into {TRIVY_DIR} but couldn't locate its binary afterward")

    # No make_tree_executable() call needed here, unlike java.py/
    # sonar_scanner.py - unlike zipfile (which loses Unix exec bits,
    # what that helper exists to fix), tarfile.extractall() preserves
    # them correctly (confirmed live), and Trivy's macOS/Linux releases
    # are always .tar.gz - only Windows uses .zip, where POSIX exec bits
    # don't apply at all. A no-op call here would just be misleading
    # copy-paste, not a real safeguard.
    verify_runnable(trivy_bin, "--version")

    success(f"trivy ready: {trivy_bin}")
    return trivy_bin
