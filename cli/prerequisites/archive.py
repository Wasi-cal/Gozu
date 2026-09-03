"""
Shared download/extract/verify helpers for managed host dependencies (see
java.py, sonar_scanner.py) - a future ensure_*() for a new host dependency
should reuse these, not reimplement its own download loop.
"""

import platform
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

import requests

CODESCAN_HOME = Path.home() / ".codescan"


def os_name(labels: dict[str, str]) -> str:
    """Map platform.system() to one of `labels`' keys, or raise if unsupported."""
    system = platform.system()
    if system not in labels:
        raise RuntimeError(f"No managed download available for this OS: {system} (supported: {', '.join(labels)})")
    return labels[system]


def arch_name() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("arm64", "aarch64"):
        return "aarch64"
    raise RuntimeError(f"No managed download available for this CPU architecture: {machine}")


def download_archive(url: str, suffix: str, timeout: int) -> Path:
    """Stream `url` to a temp file and return its path - caller is responsible for deleting it."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        with requests.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                tmp.write(chunk)
    return tmp_path


def extract_archive(archive_path: Path, dest_dir: Path, *, is_zip: bool) -> None:
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


def make_tree_executable(root: Path) -> None:
    """
    zipfile.extractall() doesn't preserve Unix executable bits. Fix every
    file under every "bin" directory in the extracted tree - not just one
    known binary, since a distribution can bundle its own embedded runtime
    whose binaries need the same treatment.
    """
    if platform.system() == "Windows":
        return
    for bin_dir in root.rglob("bin"):
        if bin_dir.is_dir():
            for entry in bin_dir.iterdir():
                if entry.is_file():
                    entry.chmod(entry.stat().st_mode | 0o111)


def verify_runnable(binary: Path, *version_args: str, env: dict[str, str] | None = None) -> None:
    result = subprocess.run([str(binary), *version_args], capture_output=True, text=True, check=False, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"Downloaded binary at {binary} failed to run: {result.stderr}")
