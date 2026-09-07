# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""Small plain constants shared across gozu that don't belong to any one module's own domain."""

# Who a person reporting an unexpected gozu crash should reach - used to
# build the CLI's pre-filled mailto: link (cli/crash_handler.py) and
# appears as plain text alongside the worker's own crash logging
# (temporal/worker.py) for the same purpose where no terminal is attached
# to click a link.
CONTACT_EMAILS = ["wasiullah.rafeeqs@calfus.com", "prakrit.mohanty@calfus.com"]
