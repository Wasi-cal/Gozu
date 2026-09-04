# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""HMAC signature verification for SonarQube webhook payloads."""

import hashlib
import hmac


def verify_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """
    SonarQube signs the raw request body with HMAC-SHA256 using the webhook
    secret, and sends the hex digest in X-Sonar-Webhook-HMAC-SHA256.
    """
    if not signature_header:
        return False

    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
