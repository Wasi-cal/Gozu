# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: Jira (Atlassian) - direct API client

"""Jira implementation of TicketClient (see ticket/base.py for the contract)."""

from pathlib import Path
from typing import Any

import requests
from temporalio import activity

from core.errors import TicketAuthError, TicketValidationError
from core.models import Finding, Severity
from ticket.adf import bullet_list, code_block, doc, paragraph
from ticket.base import (
    DEFAULT_PRIORITY,
    SEVERITY_TO_PRIORITY,
    SUMMARY_MAX_LENGTH,
    TicketClient,
)
from ticket.jira_sprint import SprintAssigner

# Distinct from the per-finding "source-key-{key}" labels - identifies the
# one shared backlog-rollup ticket a project can have (see
# upsert_rollup_ticket()), so it can be found again on a later run instead
# of creating a second one.
ROLLUP_LABEL = "gozu-backlog-rollup"

# How many of the deferred findings' keys/rules/severities to actually
# list in the rollup ticket's description before summarizing the rest -
# a genuinely large backlog (hundreds of findings) would make for an
# unreadable ticket body otherwise.
_ROLLUP_DESCRIPTION_MAX_LINES = 50

# Exact names a Jira admin must give these two OPTIONAL custom fields
# (Short text / Number respectively - see README.md's Jira setup section)
# for discover_custom_fields() to find them. Neither existing is the
# default, unchanged-from-today state: Component/Line stay embedded in
# Description text instead.
CUSTOM_FIELD_COMPONENT_NAME = "SonarQube Component"
CUSTOM_FIELD_LINE_NAME = "SonarQube Line"


def _normalize_label_value(value: str) -> str:
    """Jira labels can't contain spaces - lowercased, spaces hyphenated, everything else (e.g. a branch's "/") left as-is since Jira accepts it."""
    return value.strip().lower().replace(" ", "-")


class JiraClient(TicketClient):
    def __init__(self, base_url: str, email: str, api_token: str, project_key: str):
        self.base_url = base_url.rstrip("/")
        self.project_key = project_key
        self.auth = (email, api_token)
        self.headers = {"Content-Type": "application/json"}
        self._sprints = SprintAssigner(self.base_url, self.auth, self.headers, project_key)

    def destination_id(self) -> str:
        return f"jira:{self.base_url}:{self.project_key}"

    def _raise_for_status(self, response: requests.Response, action: str) -> None:
        """
        Shared by every Jira call this client makes - one place to
        distinguish permanent failures (never worth retrying) from
        everything else (404s, 429s, 5xxs, left as a generic RuntimeError,
        same retryable path as before this existed).
        """
        if 200 <= response.status_code < 300:
            return
        if response.status_code in (401, 403):
            raise TicketAuthError(f"Jira {action} failed with status {response.status_code} (invalid/expired token?): {response.text}")
        if response.status_code == 400:
            raise TicketValidationError(f"Jira {action} failed with status {response.status_code}: {response.text}")
        raise RuntimeError(f"Jira {action} failed with status {response.status_code}: {response.text}")

    def ticket_exists(self, ticket_key: str) -> bool:
        """
        Whether `ticket_key` still exists in Jira - True on a normal GET,
        False specifically on 404 (deleted, or never existed). Any other
        non-2xx (401/403/429/5xx/...) still raises via _raise_for_status()
        rather than being treated as "gone" - a transient/auth failure
        must never be misread as evidence the ticket was deleted.
        """
        response = requests.get(
            f"{self.base_url}/rest/api/3/issue/{ticket_key}",
            params={"fields": "key"},
            auth=self.auth,
            headers=self.headers,
        )
        if response.status_code == 404:
            return False
        self._raise_for_status(response, "get issue")
        return True

    def _find_by_label(self, label: str) -> str | None:
        """Search for a Jira ticket tagged with `label` in this project. Returns the issue key (e.g. "PROJ-123") if found, else None."""
        jql = f'project = {self.project_key} AND labels = "{label}"'

        params: dict[str, Any] = {"jql": jql, "fields": "key", "maxResults": 1}
        response = requests.get(
            f"{self.base_url}/rest/api/3/search/jql",
            params=params,
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(response, "search")

        issues = response.json().get("issues", [])
        return issues[0]["key"] if issues else None

    def find_existing(self, finding_key: str) -> str | None:
        """Already-ticketed finding? (dedupe) - source-key-{key} is stamped on every per-finding ticket at creation."""
        return self._find_by_label(f"source-key-{finding_key}")

    def _map_priority(self, severity: Severity) -> str:
        return SEVERITY_TO_PRIORITY.get(severity, DEFAULT_PRIORITY)

    def _build_summary(self, finding: Finding) -> str:
        summary = finding.title
        if len(summary) > SUMMARY_MAX_LENGTH:
            summary = summary[: SUMMARY_MAX_LENGTH - 3] + "..."
        return summary

    def _build_labels(self, finding: Finding) -> list[str]:
        """
        "source-sonarqube"/"type-{type}" identify the scanner/finding kind
        the same way "security" already did, just structured (Source/Type/
        Branch) instead of ad hoc - all show natively in Jira's Details
        panel with zero project setup, unlike the custom fields below.
        "branch-{branch}" is only added when a real branch value exists -
        a config/scanner with no branch concept (e.g. SonarQube Cloud
        Free, always "main" and never tagged - see
        cli/scan_runner/scanner_exec.py's _build_scanner_command()) gets
        no branch label at all rather than a meaningless "branch-none".
        """
        labels = [
            "source-sonarqube",
            "security",
            f"type-{_normalize_label_value(finding.finding_type)}",
            f"source-key-{finding.key}",
        ]
        if finding.branch:
            labels.append(f"branch-{_normalize_label_value(finding.branch)}")
        return labels

    def _build_description(self, finding: Finding, component_moved: bool = False, line_moved: bool = False) -> dict:
        """
        `component_moved`/`line_moved` are True when that value is being
        set as a real custom field instead (see create_ticket()) - the
        corresponding line is dropped here so it isn't shown twice.
        deep_link is never included here at all anymore - it's a native
        remote link now (create_ticket()'s _create_remote_link() call),
        not embedded text.
        """
        details = []
        if not component_moved:
            details.append(f"Component: {finding.component}")
        if not line_moved:
            details.append(f"Line: {finding.line}")
        details += [
            f"Type: {finding.finding_type}",
            f"Severity: {finding.severity.value}",
            f"Source: {finding.source_tool}",
            f"Branch: {finding.branch or 'unknown'}",
        ]
        return doc(paragraph(finding.message), bullet_list(details))

    def discover_custom_fields(self, names: list[str]) -> dict[str, str]:
        """
        Queries Jira's full field list ONCE (see create_tickets_activity,
        which calls this a single time per activity execution, not once
        per ticket - the same Jira instance backs every ticket in a run)
        and returns whichever of `names` (exact match against Jira's
        field "name") actually exist in this instance, as
        name -> "customfield_XXXXX". A name with no match simply isn't a
        key in the returned dict - this is a lookup, not a requirement;
        create_ticket() treats a missing name as "keep that content in
        Description", exactly like today.

        Existing globally is NOT the same as being usable - a field can
        exist in this Jira instance but not be on this project's
        create/edit screen, which this list-all-fields query has no way
        to detect. See create_ticket()'s write-time fallback for the case
        this can't catch.
        """
        response = requests.get(f"{self.base_url}/rest/api/3/field", auth=self.auth, headers=self.headers)
        self._raise_for_status(response, "list fields")
        wanted = set(names)
        return {field["name"]: field["id"] for field in response.json() if field.get("name") in wanted}

    def _build_create_payload(
        self, finding: Finding, component_field_id: str | None, line_field_id: str | None
    ) -> dict[str, Any]:
        line_value = finding.line if (line_field_id and finding.line is not None) else None
        fields: dict[str, Any] = {
            "project": {"key": self.project_key},
            "summary": self._build_summary(finding),
            "issuetype": {"name": "Bug"},
            "priority": {"name": self._map_priority(finding.severity)},
            "labels": self._build_labels(finding),
            "description": self._build_description(
                finding, component_moved=bool(component_field_id), line_moved=line_value is not None
            ),
        }
        if component_field_id:
            fields[component_field_id] = finding.component
        if line_value is not None:
            fields[line_field_id] = line_value
        return {"fields": fields}

    def _rejected_custom_field_ids(self, response: requests.Response, candidate_ids: set[str]) -> set[str]:
        """
        Parses a 400 create-issue response for Jira's per-field "errors"
        object, returning whichever of `candidate_ids` (the customfield_
        XXXXX ids create_ticket() actually tried to set) Jira rejected -
        empty if the response has no per-field errors at all, OR if it
        names anything NOT in `candidate_ids` (a genuinely unrelated
        validation failure - a bad project key, an invalid issue type -
        which must keep raising TicketValidationError exactly as before,
        never be silently retried away just because a custom field was
        also involved).
        """
        try:
            body = response.json()
        except ValueError:
            return set()
        error_keys = set((body.get("errors") or {}).keys())
        if not error_keys or not error_keys <= candidate_ids:
            return set()
        return error_keys

    def _create_remote_link(self, issue_key: str, url: str) -> None:
        payload: dict[str, Any] = {"object": {"url": url, "title": "SonarQube finding"}}
        response = requests.post(
            f"{self.base_url}/rest/api/3/issue/{issue_key}/remotelink",
            json=payload,
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(response, "create remote link")

    def create_ticket(self, finding: Finding, custom_fields: dict[str, str] | None = None) -> str:
        """
        Create a Jira issue for a finding, return the new issue key.

        `custom_fields` (from discover_custom_fields(), called once per
        create_tickets_activity run, not per ticket) is whichever of
        CUSTOM_FIELD_COMPONENT_NAME/CUSTOM_FIELD_LINE_NAME this Jira
        instance actually has - per-field, not all-or-nothing: only the
        ones present get set as real custom fields, the rest stay in
        Description.

        Existing in this Jira instance doesn't guarantee usable on THIS
        project's create screen - if Jira's response rejects the create
        specifically because of one or both custom fields (parsed by
        _rejected_custom_field_ids(), not just any 400), this retries the
        exact same creation with the rejected field(s) removed and that
        content folded back into Description instead, logging clearly so
        a misconfigured field is visible rather than silently degraded.
        A 400 for any other reason is untouched - _raise_for_status()
        raises TicketValidationError exactly as it always has.
        """
        custom_fields = custom_fields or {}
        component_field_id = custom_fields.get(CUSTOM_FIELD_COMPONENT_NAME)
        line_field_id = custom_fields.get(CUSTOM_FIELD_LINE_NAME)

        payload = self._build_create_payload(finding, component_field_id, line_field_id)
        response = requests.post(
            f"{self.base_url}/rest/api/3/issue",
            json=payload,
            auth=self.auth,
            headers=self.headers,
        )

        if response.status_code == 400 and (component_field_id or line_field_id):
            candidate_ids = {fid for fid in (component_field_id, line_field_id) if fid}
            rejected = self._rejected_custom_field_ids(response, candidate_ids)
            if rejected:
                activity.logger.warning(
                    f"Jira rejected custom field(s) {sorted(rejected)} for finding {finding.key} "
                    "(present in this Jira instance but not on this project's create screen?) - "
                    "retrying with that content folded back into Description"
                )
                if component_field_id in rejected:
                    component_field_id = None
                if line_field_id in rejected:
                    line_field_id = None
                payload = self._build_create_payload(finding, component_field_id, line_field_id)
                response = requests.post(
                    f"{self.base_url}/rest/api/3/issue",
                    json=payload,
                    auth=self.auth,
                    headers=self.headers,
                )

        self._raise_for_status(response, "create issue")
        issue_key = response.json()["key"]

        # The issue above is already created in Jira at this point - sprint
        # assignment is a bonus, best-effort step (see ticket/jira_sprint.py's
        # own fallbacks for "no board"/"no active sprint"/Kanban), and must
        # never be able to fail ticket creation itself. Any other failure
        # here (a network blip, an unexpected Jira response) gets the same
        # treatment: log and move on, not raise.
        try:
            self._sprints.add_issue(issue_key)
        except Exception as e:
            activity.logger.warning(f"Sprint assignment failed for {issue_key}, leaving it in the backlog: {e}")

        # Native remote link, not embedded Description text - a core Jira
        # platform capability, always attempted regardless of custom-field
        # discovery, same best-effort treatment as sprint assignment above.
        try:
            self._create_remote_link(issue_key, finding.deep_link)
        except Exception as e:
            activity.logger.warning(f"Remote link creation failed for {issue_key}, deep link not attached: {e}")

        return issue_key

    def attach_screenshot(self, issue_key: str, image_path: Path) -> None:
        """
        Attach a screenshot file to an existing issue. Skips the upload if a
        file with the same name is already attached, so retries of the
        calling activity don't create duplicate attachments.
        """
        get_response = requests.get(
            f"{self.base_url}/rest/api/3/issue/{issue_key}",
            params={"fields": "attachment"},
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(get_response, "get attachments")

        existing = get_response.json().get("fields", {}).get("attachment", [])
        if any(attachment.get("filename") == image_path.name for attachment in existing):
            activity.logger.warning(
                f"Attachment {image_path.name} already exists on {issue_key}; skipping upload"
            )
            return

        with open(image_path, "rb") as f:
            response = requests.post(
                f"{self.base_url}/rest/api/3/issue/{issue_key}/attachments",
                auth=self.auth,
                headers={"X-Atlassian-Token": "no-check"},
                files={"file": (image_path.name, f, "image/png")},
            )
        self._raise_for_status(response, "attach screenshot")

    def add_comment(self, ticket_key: str, body: str) -> None:
        """Add a comment to an existing ticket, rendering `body` as a single Jira code-block node."""
        payload: dict[str, Any] = {"body": doc(code_block(body))}
        response = requests.post(
            f"{self.base_url}/rest/api/3/issue/{ticket_key}/comment",
            json=payload,
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(response, "add comment")

    def get_transitions(self, issue_key: str) -> list[dict[str, Any]]:
        """Every transition currently available on `issue_key`, in whatever workflow this project actually uses."""
        response = requests.get(
            f"{self.base_url}/rest/api/3/issue/{issue_key}/transitions",
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(response, "get transitions")
        return response.json().get("transitions", [])

    def transition_to_done(self, issue_key: str) -> bool:
        """
        Move `issue_key` to whichever available transition leads to a
        "done"-category status - workflows vary per project, so this
        deliberately never hardcodes a status name like "Done"/"Closed",
        only the statusCategory.key Jira itself guarantees. Returns False
        (and does nothing) if no such transition is currently available,
        rather than guessing at the wrong one.
        """
        for transition in self.get_transitions(issue_key):
            if transition.get("to", {}).get("statusCategory", {}).get("key") != "done":
                continue
            response = requests.post(
                f"{self.base_url}/rest/api/3/issue/{issue_key}/transitions",
                json={"transition": {"id": transition["id"]}},
                auth=self.auth,
                headers=self.headers,
            )
            self._raise_for_status(response, "transition issue")
            return True
        return False

    def _update_ticket(self, issue_key: str, summary: str, description: dict) -> None:
        payload = {"fields": {"summary": summary, "description": description}}
        response = requests.put(
            f"{self.base_url}/rest/api/3/issue/{issue_key}",
            json=payload,
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(response, "update issue")

    def _build_rollup_summary(self, count: int) -> str:
        return f"SonarQube backlog: {count} additional finding(s) not yet ticketed"

    def _build_rollup_description(self, remaining: list[Finding]) -> dict:
        lines = [
            f"{finding.key} - {finding.finding_type} - {finding.severity.value} - {finding.title}"
            for finding in remaining[:_ROLLUP_DESCRIPTION_MAX_LINES]
        ]
        if len(remaining) > _ROLLUP_DESCRIPTION_MAX_LINES:
            lines.append(f"... and {len(remaining) - _ROLLUP_DESCRIPTION_MAX_LINES} more")

        return doc(
            paragraph(
                "These findings were detected but not individually ticketed this run "
                "(per-run backlog cap reached) - they'll get their own ticket automatically "
                "in a future run as capacity frees up."
            ),
            bullet_list(lines),
        )

    def upsert_rollup_ticket(self, remaining: list[Finding]) -> str | None:
        """
        One shared ticket for however many findings didn't get their own
        this run (see create_tickets_activity's BACKLOG_CAP) - never one
        ticket per remaining finding. Checked by ROLLUP_LABEL every call:
        an existing rollup ticket gets its summary/description updated in
        place (count included) rather than a new one created alongside it
        - --watch/webhook mode re-runs this every cycle against what's
        likely the same persistent backlog, so this must never spam a new
        "N more findings" ticket per run. `remaining` empty with an
        existing rollup ticket still updates it (down to 0), reflecting
        the backlog actually shrinking; empty with no existing ticket is a
        no-op - nothing to create for a backlog that isn't there.
        """
        existing = self._find_by_label(ROLLUP_LABEL)
        if not remaining and existing is None:
            return None

        summary = self._build_rollup_summary(len(remaining))
        description = self._build_rollup_description(remaining)

        if existing is not None:
            self._update_ticket(existing, summary=summary, description=description)
            return existing

        payload: dict[str, Any] = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": summary,
                "issuetype": {"name": "Task"},
                "labels": ["source-sonarqube", ROLLUP_LABEL],
                "description": description,
            }
        }
        response = requests.post(
            f"{self.base_url}/rest/api/3/issue",
            json=payload,
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(response, "create rollup issue")
        return response.json()["key"]
