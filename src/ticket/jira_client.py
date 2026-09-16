# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty
#
# Depends on: Jira (Atlassian) - direct API client
# Depends on: src/ticket/adf.py - ADF document builders for issue descriptions/comments
# Depends on: src/ticket/jira_sprint.py - SprintAssigner for active-sprint issue assignment

"""Jira implementation of TicketClient (see src/ticket/base.py for the contract)."""

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

# Caps how many deferred findings get listed in the rollup description
# before summarizing the rest, so a large backlog stays readable.
_ROLLUP_DESCRIPTION_MAX_LINES = 50

# Exact names a Jira admin gives these OPTIONAL custom fields (see README.md's
# Jira setup section) for discover_custom_fields() to find them - unset,
# Component/Line/Severity stay embedded in Description text instead.
CUSTOM_FIELD_COMPONENT_NAME = "SonarQube Component"
CUSTOM_FIELD_LINE_NAME = "SonarQube Line"
# The raw SonarQube severity, distinct from Jira's own translated `priority`
# (SEVERITY_TO_PRIORITY) - lets someone surface the untranslated value as a
# real field instead of just text.
CUSTOM_FIELD_SEVERITY_NAME = "SonarQube Severity"


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
        """Shared by every Jira call - distinguishes permanent failures
        (never worth retrying) from everything else (left as RuntimeError)."""
        if 200 <= response.status_code < 300:
            return
        if response.status_code in (401, 403):
            raise TicketAuthError(f"Jira {action} failed with status {response.status_code} (invalid/expired token?): {response.text}")
        if response.status_code == 400:
            raise TicketValidationError(f"Jira {action} failed with status {response.status_code}: {response.text}")
        raise RuntimeError(f"Jira {action} failed with status {response.status_code}: {response.text}")

    def ticket_exists(self, ticket_key: str) -> bool:
        """True on a normal GET, False only on 404 - any other non-2xx
        still raises, never misread as "deleted"."""
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
        """Load-bearing labels: "source-key-{finding.key}" is the actual
        dedupe mechanism (find_existing() searches by it - Jira has no
        external-ID concept). "source-{source_tool}" is dynamic, not a
        hardcoded string - with two scanners feeding the same project now,
        it's what makes "which scanner produced this ticket" filterable in
        Jira's own JQL. "branch-{branch}" is added only when a real branch
        exists."""
        labels = [
            f"source-{_normalize_label_value(finding.source_tool)}",
            f"source-key-{finding.key}",
        ]
        if finding.branch:
            labels.append(f"branch-{_normalize_label_value(finding.branch)}")
        return labels

    def _build_description(
        self,
        finding: Finding,
        component_moved: bool = False,
        line_moved: bool = False,
        severity_moved: bool = False,
    ) -> dict:
        """`*_moved` is True when that value is set as a real custom field
        instead, so it's dropped here to avoid showing it twice. deep_link
        is a native remote link now (create_ticket()), never embedded text.

        Branches on `finding.package_name`, never `finding.source_tool` -
        a package-level finding (Trivy: no file+line, just package/
        installed-version/fixed-version) gets those bullets instead of
        Line, keeping this scanner-agnostic per ticket/base.py's contract."""
        details = []
        if not component_moved:
            details.append(f"Component: {finding.component}")
        if finding.package_name:
            details.append(f"Package: {finding.package_name}")
            details.append(f"Installed version: {finding.installed_version}")
            details.append(f"Fixed version: {finding.fixed_version or 'not yet available'}")
        elif not line_moved:
            details.append(f"Line: {finding.line}")
        details.append(f"Type: {finding.finding_type}")
        if not severity_moved:
            details.append(f"Severity: {finding.severity.value}")
        details += [
            f"Source: {finding.source_tool}",
            f"Branch: {finding.branch or 'unknown'}",
        ]
        # llm_explanation, when present, replaces the raw message with an
        # LLM-generated explanation + suggested fix.
        content = [paragraph(finding.llm_explanation or finding.message), bullet_list(details)]
        if finding.how_to_fix:
            # Rule-level guidance, not tailored to this line. code_block()
            # keeps SonarQube's prose+code rule descriptions legible after
            # src/scanner/html_text.py strips the original HTML formatting.
            content.append(paragraph("How to fix:"))
            content.append(code_block(finding.how_to_fix))
        return doc(*content)

    def discover_custom_fields(self, names: list[str]) -> dict[str, str]:
        """Called once per activity run, not per ticket. Returns whichever of
        `names` exist in this instance as name -> "customfield_XXXXX"; a
        missing name isn't an error, create_ticket() just keeps that
        content in Description. Existing globally != usable on this
        project's screen - see create_ticket()'s write-time fallback."""
        response = requests.get(f"{self.base_url}/rest/api/3/field", auth=self.auth, headers=self.headers)
        self._raise_for_status(response, "list fields")
        wanted = set(names)
        return {field["name"]: field["id"] for field in response.json() if field.get("name") in wanted}

    def _build_create_payload(
        self,
        finding: Finding,
        component_field_id: str | None,
        line_field_id: str | None,
        severity_field_id: str | None = None,
    ) -> dict[str, Any]:
        line_value = finding.line if (line_field_id and finding.line is not None) else None
        fields: dict[str, Any] = {
            "project": {"key": self.project_key},
            "summary": self._build_summary(finding),
            "issuetype": {"name": "Bug"},
            "priority": {"name": self._map_priority(finding.severity)},
            "labels": self._build_labels(finding),
            "description": self._build_description(
                finding,
                component_moved=bool(component_field_id),
                line_moved=line_value is not None,
                severity_moved=bool(severity_field_id),
            ),
        }
        if component_field_id:
            fields[component_field_id] = finding.component
        if line_field_id and line_value is not None:
            fields[line_field_id] = line_value
        if severity_field_id:
            fields[severity_field_id] = finding.severity.value
        return {"fields": fields}

    def _rejected_custom_field_ids(self, response: requests.Response, candidate_ids: set[str]) -> set[str]:
        """Parses a 400 create-issue response for which `candidate_ids` Jira
        rejected. Empty if the errors name anything outside `candidate_ids` -
        an unrelated validation failure must keep raising normally, never
        get silently retried just because a custom field was also involved."""
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
        """Create a Jira issue for a finding, return the new issue key.

        `custom_fields` is per-field, not all-or-nothing - only the ones
        present in this instance get set as real fields, the rest stay in
        Description. If Jira rejects the create because of one or both
        custom fields, this retries once with the rejected field(s) folded
        back into Description; any other 400 raises normally."""
        custom_fields = custom_fields or {}
        component_field_id = custom_fields.get(CUSTOM_FIELD_COMPONENT_NAME)
        line_field_id = custom_fields.get(CUSTOM_FIELD_LINE_NAME)
        severity_field_id = custom_fields.get(CUSTOM_FIELD_SEVERITY_NAME)

        payload = self._build_create_payload(finding, component_field_id, line_field_id, severity_field_id)
        response = requests.post(
            f"{self.base_url}/rest/api/3/issue",
            json=payload,
            auth=self.auth,
            headers=self.headers,
        )

        if response.status_code == 400 and (component_field_id or line_field_id or severity_field_id):
            candidate_ids = {fid for fid in (component_field_id, line_field_id, severity_field_id) if fid}
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
                if severity_field_id in rejected:
                    severity_field_id = None
                payload = self._build_create_payload(finding, component_field_id, line_field_id, severity_field_id)
                response = requests.post(
                    f"{self.base_url}/rest/api/3/issue",
                    json=payload,
                    auth=self.auth,
                    headers=self.headers,
                )

        self._raise_for_status(response, "create issue")
        issue_key = response.json()["key"]

        # Best-effort, must never fail ticket creation itself - see
        # src/ticket/jira_sprint.py's own no-board/no-active-sprint fallbacks.
        try:
            self._sprints.add_issue(issue_key)
        except Exception as e:
            activity.logger.warning(f"Sprint assignment failed for {issue_key}, leaving it in the backlog: {e}")

        # Native remote link, same best-effort treatment as sprint assignment.
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
        """Moves to whichever transition leads to a "done"-category status -
        never hardcodes a status name, since workflows vary per project.
        Returns False if no such transition is currently available."""
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
        """One shared ticket for the whole backlog, never one per finding.
        An existing rollup ticket (found via ROLLUP_LABEL) gets updated in
        place rather than duplicated, so repeated runs never spam new
        tickets. Empty `remaining` still updates an existing ticket down to
        0; with no existing ticket it's a no-op."""
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
