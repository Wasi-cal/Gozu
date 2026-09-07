# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty, Claude
#
# Depends on: cli/prerequisites/archive.py - shared download/extract/verify helpers
# Depends on: cli/prerequisites/java.py - the Java environment sonar-scanner runs under

"""Ensuring the sonar-scanner CLI is available on the host."""

import platform
import shutil
import subprocess
from pathlib import Path

import typer

from cli.prerequisites.archive import (
    arch_name,
    download_archive,
    extract_archive,
    make_tree_executable,
    os_name,
    verify_runnable,
)
from cli.prerequisites.java import java_env
from cli.status import success, waiting
from scripts.paths import GOZU_HOME

SONAR_SCANNER_DIR = GOZU_HOME / "sonar-scanner"

# Sonar doesn't publish a "latest" alias for sonar-scanner-cli downloads (the
# distribution bucket has no directory listing), so this is pinned to a
# specific, verified-working release - bump it here if a newer one is needed.
# https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/scanners/sonarscanner
_SONAR_SCANNER_VERSION = "8.1.0.6389"


def _managed_sonar_scanner_binary() -> Path | None:
    """The sonar-scanner binary inside our own ~/.gozu/sonar-scanner/, if we've downloaded one before."""
    if not SONAR_SCANNER_DIR.is_dir():
        return None

    binary_name = "sonar-scanner.bat" if platform.system() == "Windows" else "sonar-scanner"
    for entry in SONAR_SCANNER_DIR.iterdir():
        candidate = entry / "bin" / binary_name
        if entry.is_dir() and candidate.is_file():
            return candidate
    return None


def check_sonar_scanner() -> bool:
    """
    True if a working sonar-scanner is found either on PATH or in our own
    ~/.gozu/sonar-scanner/ (checking both here, unlike check_java(),
    since ensure_sonar_scanner() relies on this one function to know about
    a previously-managed download instead of layering that check itself).
    """
    binary = shutil.which("sonar-scanner") or _managed_sonar_scanner_binary()
    if binary is None:
        return False
    try:
        result = subprocess.run([str(binary), "-v"], capture_output=True, text=True, check=False, env=java_env())
        return result.returncode == 0
    except OSError:
        return False


def ensure_sonar_scanner() -> Path:
    """
    Return a path to a usable `sonar-scanner` binary, downloading one if
    needed - same fully-contained-directory approach as ensure_java(): if
    check_sonar_scanner() finds a working one already (PATH or a previous
    managed download), use it; otherwise download the official sonar-scanner
    CLI distribution for this host's OS/arch into ~/.gozu/sonar-scanner/
    (no system-wide install) and confirm it's runnable afterward.
    """
    if check_sonar_scanner():
        existing = shutil.which("sonar-scanner") or _managed_sonar_scanner_binary()
        if existing is None:
            raise RuntimeError("check_sonar_scanner() succeeded but no binary could be located")
        return Path(existing)

    typer.echo("sonar-scanner CLI wasn't found on this machine.")
    os_label = os_name({"Darwin": "macosx", "Linux": "linux", "Windows": "windows"})
    arch = arch_name()
    waiting(f"Downloading sonar-scanner-cli {_SONAR_SCANNER_VERSION} ({os_label}/{arch}) into {SONAR_SCANNER_DIR} ...")

    url = (
        f"https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/"
        f"sonar-scanner-cli-{_SONAR_SCANNER_VERSION}-{os_label}-{arch}.zip"
    )

    SONAR_SCANNER_DIR.mkdir(parents=True, exist_ok=True)
    archive = download_archive(url, suffix=".zip", timeout=180)
    extract_archive(archive, SONAR_SCANNER_DIR, is_zip=True)

    scanner_bin = _managed_sonar_scanner_binary()
    if scanner_bin is None:
        raise RuntimeError(f"Downloaded sonar-scanner into {SONAR_SCANNER_DIR} but couldn't locate its binary afterward")

    # The distribution bundles its own embedded JRE (used unconditionally by
    # the launcher script, regardless of JAVA_HOME) - make_tree_executable
    # covers that bundled jre/bin/ too, not just the sonar-scanner script.
    make_tree_executable(SONAR_SCANNER_DIR)
    verify_runnable(scanner_bin, "-v", env=java_env())

    success(f"sonar-scanner ready: {scanner_bin}")
    return scanner_bin
