# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Exceptions distinguishing permanent failures (retrying can never help) from
transient ones (a retry might succeed) - used to configure
temporalio.common.RetryPolicy's non_retryable_error_types on
fetch_findings_activity/create_tickets_activity (see
temporal/workflows/scan_to_ticket.py).

Confirmed directly against the installed temporalio SDK (1.32.0) before
relying on this - see retry_logic.rs's should_retry() (matches
application_failure.type against each configured pattern with plain
case-insensitive STRING EQUALITY, no hierarchy/MRO walking at all) and
converter/_failure_converter.py's DefaultFailureConverter.to_failure()
(sets that `type` string to exception.__class__.__name__ - the exact,
most-derived raised class, never a base class). Net effect:
non_retryable_error_types=["ScannerAuthError"] matches an *exact*
ScannerAuthError instance and nothing else - not some other GozuError
subclass, even though GozuError is its base class. That's why every
concrete exception below is named individually in each activity's
RetryPolicy (see scan_to_ticket.py) instead of relying on GozuError
alone - a shared base here is for isinstance-check convenience elsewhere
in the code, not for Temporal's matching, which never sees it.
"""


class GozuError(Exception):
    """Shared base for gozu's own exceptions - organizational/isinstance-check purposes only (see module docstring for why this is NOT what Temporal's retry-policy matching uses)."""


class ScannerAuthError(GozuError):
    """A 401/403 from the scanner backend (e.g. SonarQube) - an invalid/expired/revoked token, never fixed by retrying."""


class TicketAuthError(GozuError):
    """A 401/403 from the ticket backend (e.g. Jira) - same reasoning as ScannerAuthError, distinct class since the two retry policies are configured independently."""


class TicketValidationError(GozuError):
    """A 400 from the ticket backend (e.g. Jira) - a permanently malformed request (bad project key, invalid issue type, invalid field value), not something a retry could ever fix."""
