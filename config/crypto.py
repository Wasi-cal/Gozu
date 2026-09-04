# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Symmetric encryption for secrets stored in the configs table (sonar_token,
jira_api_token, webhook_secret) - so a Postgres dump or a stray SELECT never
exposes plaintext credentials.

Uses Fernet (AES-128-CBC + HMAC, from the `cryptography` library), keyed by
FERNET_KEY. Generate a key with `Fernet.generate_key()` (scripts/bootstrap_env.py
does this automatically) - losing FERNET_KEY makes every encrypted value in
the database permanently unrecoverable, there is no recovery path.
"""

import os

from cryptography.fernet import Fernet, InvalidToken


class MissingFernetKeyError(RuntimeError):
    def __init__(self):
        super().__init__(
            "FERNET_KEY environment variable is not set. Generate one with "
            "scripts/bootstrap_env.py, or manually via "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`."
        )


class InvalidFernetKeyError(RuntimeError):
    def __init__(self, cause: Exception):
        super().__init__(
            f"FERNET_KEY environment variable is not a valid Fernet key: {cause}"
        )


def _get_fernet() -> Fernet:
    key = os.environ.get("FERNET_KEY")
    if not key:
        raise MissingFernetKeyError()

    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as e:
        raise InvalidFernetKeyError(e) from e


def encrypt_token(plaintext: str) -> str:
    """Encrypt a plaintext secret, returning a Fernet token as a str."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a Fernet token (as produced by encrypt_token) back to plaintext."""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise ValueError(
            "Failed to decrypt value: not a valid Fernet token for the current FERNET_KEY "
            "(wrong key, or the value is corrupted/not actually encrypted)"
        ) from e
