"""Free-port probing and .env file parsing, shared by scripts/bootstrap_env.py and cli/init_wizard."""

import os
import socket
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Host-side default ports.
DEFAULT_PORTS: dict[str, int] = {
    "POSTGRES_PORT": 5432,
    "SONARQUBE_PORT": 9000,
    "TEMPORAL_PORT": 7233,
    "TEMPORAL_UI_PORT": 8233,
    "RECEIVER_PORT": 5000,
}


def _port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) != 0


def find_free_port(preferred: int) -> int:
    """Return `preferred` if nothing's listening on it, else the next free port above it."""
    port = preferred
    while not _port_is_free(port):
        port += 1
    return port


def resolve_ports() -> dict[str, int]:
    """Probe each default port, auto-incrementing past whatever's already taken."""
    return {name: find_free_port(default) for name, default in DEFAULT_PORTS.items()}


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse an existing .env's KEY=VALUE lines (surrounding quotes stripped) into a dict."""
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_into_environ(path: Path = ENV_PATH) -> None:
    """
    Load .env's values into this process's environment - writing .env to
    disk (bootstrap_env()) doesn't, by itself, make POSTGRES_HOST etc
    visible to os.environ for the process that just wrote it. Never
    overwrites a value already set in the environment (a real exported
    env var, or an earlier call in the same process, wins over .env).
    """
    for key, value in parse_env_file(path).items():
        os.environ.setdefault(key, value)
