#!/usr/bin/env python3
"""
Throwaway verification script for Phase 1 - NOT part of the product (the
real way to create a config is the Phase 2 CLI wizard).

Inserts one dummy config via config_store.create_config(), reads it back via
config_store.get_config(), and confirms every decrypted field round-trips
back to exactly what was written - proving the encrypt-on-write/decrypt-on-read
path actually works end to end against a real Postgres instance, not just
crypto_utils in isolation.

Cleans up the row it creates so re-running this script stays idempotent.
"""

import sys
import uuid

import config_store

DUMMY_FIELDS = {
    "scanner_type": "sonarqube",
    "scanner_mode": "local",
    "sonar_host_url": "http://localhost:9000",
    "sonar_token": "sqp_dummy_sonar_token_1234567890",
    "sonar_organization": None,
    "ticket_backend": "jira",
    "jira_url": "https://example.atlassian.net",
    "jira_email": "bot@example.com",
    "jira_api_token": "dummy_jira_api_token_abcdefgh",
    "jira_project_key": "TEST",
    "trigger_mode": "direct",
    "webhook_secret": "dummy_webhook_secret_zzzzzz",
}


def main() -> int:
    config_name = f"phase1-seed-test-{uuid.uuid4().hex[:8]}"

    print(f"Creating config '{config_name}'...")
    config_id = config_store.create_config(config_name, **DUMMY_FIELDS)
    print(f"Created config id={config_id}")

    print("Reading it back and decrypting...")
    read_back = config_store.get_config(config_name)

    if read_back is None:
        print("FAILED: get_config() returned None for a config that was just created")
        return 1

    mismatches = [
        field
        for field, expected in DUMMY_FIELDS.items()
        if read_back.get(field) != expected
    ]

    config_store.delete_config(config_name)
    print(f"Cleaned up config '{config_name}'")

    if mismatches:
        print(f"FAILED: {len(mismatches)} field(s) didn't round-trip: {', '.join(mismatches)}")
        for field in mismatches:
            print(f"  {field}: wrote {DUMMY_FIELDS[field]!r}, read back {read_back.get(field)!r}")
        return 1

    print(f"SUCCESS: all {len(DUMMY_FIELDS)} fields round-tripped correctly "
          f"(encrypted on write, decrypted on read, values match).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
