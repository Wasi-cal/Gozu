# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty

"""Ensuring a Java runtime is available for sonar-scanner - see scanner.base.ScannerRequirements.host_dependencies."""

import os
import shutil
import subprocess
from pathlib import Path

import typer

from cli.prerequisites.archive import (
    GOZU_HOME,
    arch_name,
    download_archive,
    extract_archive,
    make_tree_executable,
    os_name,
    verify_runnable,
)

JRE_DIR = GOZU_HOME / "jre"

# Adoptium (Eclipse Temurin) always publishes a "latest" build per LTS
# feature version - 21 is the current LTS at time of writing.
_ADOPTIUM_FEATURE_VERSION = 21


def check_java() -> bool:
    """Attempt `java -version` on PATH. True on success, False on any failure (missing, non-zero exit, ...)."""
    try:
        result = subprocess.run(["java", "-version"], capture_output=True, text=True, check=False)
        return result.returncode == 0
    except OSError:
        return False


def _managed_java_binary() -> Path | None:
    """The java binary inside our own ~/.gozu/jre/, if we've downloaded one before."""
    if not JRE_DIR.is_dir():
        return None

    for entry in JRE_DIR.iterdir():
        if not entry.is_dir():
            continue
        # macOS Adoptium tarballs nest the actual JRE under Contents/Home.
        for candidate in (entry / "Contents" / "Home" / "bin" / "java", entry / "bin" / "java"):
            if candidate.is_file():
                return candidate
    return None


def ensure_java() -> Path:
    """
    Return a path to a usable `java` binary, downloading one if needed.

    Checks, in order: a JRE we already downloaded into ~/.gozu/jre/
    (so re-running the wizard doesn't re-download every time), then
    whatever's on PATH (check_java()). Only if both are absent does this
    download a portable Eclipse Temurin JRE build for this host's OS/arch
    into ~/.gozu/jre/ - no system-wide install, fully contained to
    that directory.
    """
    managed = _managed_java_binary()
    if managed is not None:
        return managed

    if check_java():
        on_path = shutil.which("java")
        return Path(on_path) if on_path else Path("java")

    typer.echo("sonar-scanner needs a Java runtime (JVM), and none was found on this machine.")
    os_label = os_name({"Darwin": "mac", "Linux": "linux", "Windows": "windows"})
    arch = arch_name()
    typer.echo(f"Downloading a portable Eclipse Temurin JRE ({os_label}/{arch}) into {JRE_DIR} ...")

    url = (
        f"https://api.adoptium.net/v3/binary/latest/{_ADOPTIUM_FEATURE_VERSION}/ga/"
        f"{os_label}/{arch}/jre/hotspot/normal/eclipse?project=jdk"
    )
    is_zip = os_label == "windows"

    JRE_DIR.mkdir(parents=True, exist_ok=True)
    archive = download_archive(url, suffix=".zip" if is_zip else ".tar.gz", timeout=120)
    extract_archive(archive, JRE_DIR, is_zip=is_zip)

    java_bin = _managed_java_binary()
    if java_bin is None:
        raise RuntimeError(f"Downloaded a JRE into {JRE_DIR} but couldn't locate its java binary afterward")

    make_tree_executable(JRE_DIR)
    verify_runnable(java_bin, "-version")

    typer.secho(f"Java runtime ready: {java_bin}", fg=typer.colors.GREEN)
    return java_bin


def java_env() -> dict[str, str]:
    """
    An environment dict for running sonar-scanner (or anything else that
    shells out to `java`): JAVA_HOME/PATH point at whatever ensure_java()
    resolved to, so a managed JRE in ~/.gozu/jre/ is actually found by
    sonar-scanner's own launcher script - it's never installed/symlinked
    anywhere on the real system PATH or JAVA_HOME.
    """
    java_bin = ensure_java()
    java_home = java_bin.parent.parent
    env = os.environ.copy()
    env["JAVA_HOME"] = str(java_home)
    env["PATH"] = f"{java_bin.parent}{os.pathsep}{env.get('PATH', '')}"
    return env
