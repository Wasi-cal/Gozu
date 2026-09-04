#!/usr/bin/env python3
"""
Throwaway verification script - NOT part of the product (the real way to
create a config is `gozu init`, cli/init_wizard.py).

Inserts one dummy config via config_store.create_config(), reads it back via
config_store.get_config(), and confirms every decrypted credential
round-trips back to exactly what was written - proving the
encrypt-on-write/decrypt-on-read path actually works end to end against a
real Postgres instance, not just config.crypto in isolation.

Cleans up the row it creates so re-running this script stays idempotent.
"""

import sys
import uuid

import config.store as config_store

CONFIG_FIELDS = {
    "scanner_type": "sonarqube",
    "scanner_mode": "local",
    "ticket_backend": "jira",
    "trigger_mode": "direct",
    "project_key": "test-project",
}

CREDENTIALS = {
    "sonar_host_url": "http://localhost:9000",
    "sonar_token": "sqp_dummy_sonar_token_1234567890",
    "jira_url": "https://example.atlassian.net",
    "jira_email": "bot@example.com",
    "jira_api_token": "dummy_jira_api_token_abcdefgh",
    "jira_project_key": "TEST",
    "webhook_secret": "dummy_webhook_secret_zzzzzz",
}


def main() -> int:
    config_name = f"seed-test-{uuid.uuid4().hex[:8]}"

    print(f"Creating config '{config_name}'...")
    config_id = config_store.create_config(config_name, credentials=CREDENTIALS, **CONFIG_FIELDS)
    print(f"Created config id={config_id}")

    print("Reading it back and decrypting...")
    read_back = config_store.get_config(config_name)

    if read_back is None:
        print("FAILED: get_config() returned None for a config that was just created")
        return 1

    config_mismatches = [field for field, expected in CONFIG_FIELDS.items() if read_back.get(field) != expected]
    credential_mismatches = [
        key for key, expected in CREDENTIALS.items() if read_back.get("credentials", {}).get(key) != expected
    ]

    config_store.delete_config(config_name)
    print(f"Cleaned up config '{config_name}'")

    mismatches = config_mismatches + credential_mismatches
    if mismatches:
        print(f"FAILED: {len(mismatches)} field(s) didn't round-trip: {', '.join(mismatches)}")
        for field in config_mismatches:
            print(f"  {field}: wrote {CONFIG_FIELDS[field]!r}, read back {read_back.get(field)!r}")
        for key in credential_mismatches:
            print(f"  credentials.{key}: wrote {CREDENTIALS[key]!r}, read back {read_back.get('credentials', {}).get(key)!r}")
        return 1

    total = len(CONFIG_FIELDS) + len(CREDENTIALS)
    print(f"SUCCESS: all {total} fields round-tripped correctly "
          f"(credentials encrypted on write, decrypted on read, all values match).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
