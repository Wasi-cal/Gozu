"""
Host prerequisite checks for the init wizard and `codescan run`: Java (which
sonar-scanner needs to run at all) and the sonar-scanner CLI itself - see
ScannerRequirements.host_dependencies in scanner/client.py. A future scanner
backend with a different host dependency would get its own check_*/ensure_*
pair here, reusing _download_archive/_make_executable/_verify_runnable
below.
"""

import os
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
SONAR_SCANNER_DIR = CODESCAN_HOME / "sonar-scanner"

# Adoptium (Eclipse Temurin) always publishes a "latest" build per LTS
# feature version - 21 is the current LTS at time of writing.
_ADOPTIUM_FEATURE_VERSION = 21

# Sonar doesn't publish a "latest" alias for sonar-scanner-cli downloads (the
# distribution bucket has no directory listing), so this is pinned to a
# specific, verified-working release - bump it here if a newer one is needed.
# https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/scanners/sonarscanner
_SONAR_SCANNER_VERSION = "8.1.0.6389"


def _os_name(labels: dict[str, str]) -> str:
    """Map platform.system() to one of `labels`' keys, or raise if unsupported."""
    system = platform.system()
    if system not in labels:
        raise RuntimeError(f"No managed download available for this OS: {system} (supported: {', '.join(labels)})")
    return labels[system]


def _arch_name() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("arm64", "aarch64"):
        return "aarch64"
    raise RuntimeError(f"No managed download available for this CPU architecture: {machine}")


def _download_archive(url: str, suffix: str, timeout: int) -> Path:
    """Stream `url` to a temp file and return its path - caller is responsible for deleting it."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        with requests.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                tmp.write(chunk)
    return tmp_path


def _extract_archive(archive_path: Path, dest_dir: Path, *, is_zip: bool) -> None:
    """Extract `archive_path` (zip or tar.gz) into `dest_dir`, then delete the archive."""
    try:
        if is_zip:
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(dest_dir)
        else:
            with tarfile.open(archive_path) as tf:
                tf.extractall(dest_dir, filter="data")
    finally:
        archive_path.unlink(missing_ok=True)


def _make_tree_executable(root: Path) -> None:
    """
    zipfile.extractall() doesn't preserve Unix executable bits. Fix every
    file under every "bin" directory in the extracted tree - not just one
    known binary, since a distribution can bundle its own embedded runtime
    (see ensure_sonar_scanner()) whose binaries need the same treatment.
    """
    if platform.system() == "Windows":
        return
    for bin_dir in root.rglob("bin"):
        if bin_dir.is_dir():
            for entry in bin_dir.iterdir():
                if entry.is_file():
                    entry.chmod(entry.stat().st_mode | 0o111)


def _verify_runnable(binary: Path, *version_args: str, env: dict[str, str] | None = None) -> None:
    result = subprocess.run([str(binary), *version_args], capture_output=True, text=True, check=False, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"Downloaded binary at {binary} failed to run: {result.stderr}")


def check_java() -> bool:
    """Attempt `java -version` on PATH. True on success, False on any failure (missing, non-zero exit, ...)."""
    try:
        result = subprocess.run(["java", "-version"], capture_output=True, text=True, check=False)
        return result.returncode == 0
    except OSError:
        return False


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
    os_name = _os_name({"Darwin": "mac", "Linux": "linux", "Windows": "windows"})
    arch = _arch_name()
    typer.echo(f"Downloading a portable Eclipse Temurin JRE ({os_name}/{arch}) into {JRE_DIR} ...")

    url = (
        f"https://api.adoptium.net/v3/binary/latest/{_ADOPTIUM_FEATURE_VERSION}/ga/"
        f"{os_name}/{arch}/jre/hotspot/normal/eclipse?project=jdk"
    )
    is_zip = os_name == "windows"

    JRE_DIR.mkdir(parents=True, exist_ok=True)
    archive = _download_archive(url, suffix=".zip" if is_zip else ".tar.gz", timeout=120)
    _extract_archive(archive, JRE_DIR, is_zip=is_zip)

    java_bin = _managed_java_binary()
    if java_bin is None:
        raise RuntimeError(f"Downloaded a JRE into {JRE_DIR} but couldn't locate its java binary afterward")

    _make_tree_executable(JRE_DIR)
    _verify_runnable(java_bin, "-version")

    typer.secho(f"Java runtime ready: {java_bin}", fg=typer.colors.GREEN)
    return java_bin


def java_env() -> dict[str, str]:
    """
    An environment dict for running sonar-scanner (or anything else that
    shells out to `java`): JAVA_HOME/PATH point at whatever ensure_java()
    resolved to, so a managed JRE in ~/.codescan/jre/ is actually found by
    sonar-scanner's own launcher script - it's never installed/symlinked
    anywhere on the real system PATH or JAVA_HOME.
    """
    java_bin = ensure_java()
    java_home = java_bin.parent.parent
    env = os.environ.copy()
    env["JAVA_HOME"] = str(java_home)
    env["PATH"] = f"{java_bin.parent}{os.pathsep}{env.get('PATH', '')}"
    return env


def _managed_sonar_scanner_binary() -> Path | None:
    """The sonar-scanner binary inside our own ~/.codescan/sonar-scanner/, if we've downloaded one before."""
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
    ~/.codescan/sonar-scanner/ (checking both here, unlike check_java(),
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
    CLI distribution for this host's OS/arch into ~/.codescan/sonar-scanner/
    (no system-wide install) and confirm it's runnable afterward.
    """
    if check_sonar_scanner():
        existing = shutil.which("sonar-scanner") or _managed_sonar_scanner_binary()
        if existing is None:
            raise RuntimeError("check_sonar_scanner() succeeded but no binary could be located")
        return Path(existing)

    typer.echo("sonar-scanner CLI wasn't found on this machine.")
    os_name = _os_name({"Darwin": "macosx", "Linux": "linux", "Windows": "windows"})
    arch = _arch_name()
    typer.echo(f"Downloading sonar-scanner-cli {_SONAR_SCANNER_VERSION} ({os_name}/{arch}) into {SONAR_SCANNER_DIR} ...")

    url = (
        f"https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/"
        f"sonar-scanner-cli-{_SONAR_SCANNER_VERSION}-{os_name}-{arch}.zip"
    )

    SONAR_SCANNER_DIR.mkdir(parents=True, exist_ok=True)
    archive = _download_archive(url, suffix=".zip", timeout=180)
    _extract_archive(archive, SONAR_SCANNER_DIR, is_zip=True)

    scanner_bin = _managed_sonar_scanner_binary()
    if scanner_bin is None:
        raise RuntimeError(
            f"Downloaded sonar-scanner into {SONAR_SCANNER_DIR} but couldn't locate its binary afterward"
        )

    # The distribution bundles its own embedded JRE (used unconditionally by
    # the launcher script, regardless of JAVA_HOME) - _make_tree_executable
    # covers that bundled jre/bin/ too, not just the sonar-scanner script.
    _make_tree_executable(SONAR_SCANNER_DIR)
    _verify_runnable(scanner_bin, "-v", env=java_env())

    typer.secho(f"sonar-scanner ready: {scanner_bin}", fg=typer.colors.GREEN)
    return scanner_bin
