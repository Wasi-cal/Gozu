"""
Host prerequisite checks for the init wizard. Currently just Java, since
that's what sonar-scanner needs (see ScannerRequirements.host_dependencies
in scanner/client.py) - a future scanner backend with a different host
dependency would get its own check_*/ensure_* pair here.
"""

import platform
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

import requests
import typer

CODESCAN_HOME = Path.home() / ".codescan"
JRE_DIR = CODESCAN_HOME / "jre"

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


def _adoptium_os() -> str:
    system = platform.system()
    if system == "Darwin":
        return "mac"
    if system == "Linux":
        return "linux"
    if system == "Windows":
        return "windows"
    raise RuntimeError(f"No managed JRE available for this OS: {system}")


def _adoptium_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("arm64", "aarch64"):
        return "aarch64"
    raise RuntimeError(f"No managed JRE available for this CPU architecture: {machine}")


def _managed_java_binary() -> Path | None:
    """The java binary inside our own ~/.codescan/jre/, if we've downloaded one before."""
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

    Checks, in order: a JRE we already downloaded into ~/.codescan/jre/
    (so re-running the wizard doesn't re-download every time), then
    whatever's on PATH (check_java()). Only if both are absent does this
    explain why sonar-scanner needs a JVM and download a portable Eclipse
    Temurin JRE build for this host's OS/arch into ~/.codescan/jre/ - no
    system-wide install, fully contained to that directory.
    """
    managed = _managed_java_binary()
    if managed is not None:
        return managed

    if check_java():
        on_path = shutil.which("java")
        return Path(on_path) if on_path else Path("java")

    typer.echo("sonar-scanner needs a Java runtime (JVM), and none was found on this machine.")
    os_name = _adoptium_os()
    arch = _adoptium_arch()
    typer.echo(f"Downloading a portable Eclipse Temurin JRE ({os_name}/{arch}) into {JRE_DIR} ...")

    url = (
        f"https://api.adoptium.net/v3/binary/latest/{_ADOPTIUM_FEATURE_VERSION}/ga/"
        f"{os_name}/{arch}/jre/hotspot/normal/eclipse?project=jdk"
    )

    JRE_DIR.mkdir(parents=True, exist_ok=True)
    suffix = ".zip" if os_name == "windows" else ".tar.gz"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        with requests.get(url, stream=True, timeout=120) as response:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                tmp.write(chunk)

    try:
        if os_name == "windows":
            with zipfile.ZipFile(tmp_path) as zf:
                zf.extractall(JRE_DIR)
        else:
            with tarfile.open(tmp_path) as tf:
                tf.extractall(JRE_DIR, filter="data")
    finally:
        tmp_path.unlink(missing_ok=True)

    java_bin = _managed_java_binary()
    if java_bin is None:
        raise RuntimeError(f"Downloaded a JRE into {JRE_DIR} but couldn't locate its java binary afterward")

    if os_name != "windows":
        java_bin.chmod(java_bin.stat().st_mode | 0o111)

    result = subprocess.run([str(java_bin), "-version"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Downloaded JRE at {java_bin} failed to run: {result.stderr}")

    typer.secho(f"Java runtime ready: {java_bin}", fg=typer.colors.GREEN)
    return java_bin
