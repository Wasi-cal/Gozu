# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: SonarQube (SonarSource) - direct API client

"""
Shared issues-fetch logic for both SonarQube backends (self-hosted Server
and Cloud) - they hit the exact same /api/issues/search endpoint with the
same response shape; only the host, auth details, and a couple of extra
query params (Cloud's `organization`) differ. See scanner/sonarqube_classify.py
for why /api/hotspots/search isn't used and how a result is classified.
"""

import requests
from temporalio import activity

from core.models import Finding, Severity
from scanner.base import DEFAULT_SEVERITY, SONAR_SEVERITY_MAP
from scanner.sonarqube_classify import FORMER_HOTSPOT_TAG, is_security_relevant

# SonarQube's documented max page size for issues/search.
_PAGE_SIZE = 500

# api/issues/search's classic `resolution` field - only present once an
# issue has left OPEN/CONFIRMED/REOPENED. SonarSource's newer simplified
# `issueStatus` model overlaps some of this (e.g. FALSE_POSITIVE), but
# `resolution` (with this exact hyphenated spelling) is still returned
# today for backward compatibility, same "old and new fields coexist"
# situation as sonarqube_classify.py's type/impacts/tags triple-check.
_RESOLVED_RESOLUTIONS = {"FIXED", "REMOVED", "WONTFIX", "FALSE-POSITIVE"}


class SonarQubeIssueFetcher:
    """
    Mixin providing auth, rule-name lookup, severity mapping, title
    building, and paginated+classified issue fetching. A subclass supplies
    `_request_base_url`, `base_url`, `token`, and may override
    `_extra_params()` for backend-specific query params (Cloud's
    `organization`).
    """

    _request_base_url: str
    base_url: str
    token: str

    def __init__(self) -> None:
        self._rule_name_cache: dict[str, str] = {}

    def _auth(self) -> tuple[str, str]:
        # SonarQube web API convention: token as HTTP basic auth username, empty password.
        return (self.token, "")

    def _extra_params(self) -> dict[str, str]:
        """Extra query params every request needs - none by default, Cloud overrides for `organization`."""
        return {}

    def _fetch_rule_name(self, rule_key: str) -> str:
        if rule_key in self._rule_name_cache:
            return self._rule_name_cache[rule_key]

        name = rule_key
        try:
            response = requests.get(
                f"{self._request_base_url}/api/rules/show",
                params={"key": rule_key, **self._extra_params()},
                auth=self._auth(),
            )
            if response.status_code == 200:
                name = response.json().get("rule", {}).get("name", rule_key)
        except requests.RequestException:
            pass  # fall back to the rule key rather than fail the whole fetch

        self._rule_name_cache[rule_key] = name
        return name

    def _map_severity(self, raw_severity: str | None) -> Severity:
        if raw_severity in SONAR_SEVERITY_MAP:
            return SONAR_SEVERITY_MAP[raw_severity]
        activity.logger.warning(
            f"Unrecognized or missing severity '{raw_severity}', defaulting to '{DEFAULT_SEVERITY.value}'"
        )
        return DEFAULT_SEVERITY

    def _build_title(self, rule_name: str, component: str, line: int | None) -> str:
        """
        The rule's own display name already reads as a clean human sentence
        (e.g. "CSRF protections should not be disabled") - just append
        where it was found.
        """
        relative_path = component.split(":", 1)[-1]  # component is "{project_key}:{relative/path}"
        location = f"{relative_path}:{line}" if line is not None else relative_path
        return f"{rule_name} ({location})"

    def _fetch_all_pages(self, params: dict) -> list[dict]:
        """Follow /api/issues/search's `paging` object until every page's issues are collected."""
        items: list[dict] = []
        page = 1
        while True:
            response = requests.get(
                f"{self._request_base_url}/api/issues/search",
                params={**params, **self._extra_params(), "p": page, "ps": _PAGE_SIZE},
                auth=self._auth(),
            )
            if response.status_code != 200:
                raise RuntimeError(f"SonarQube issues/search failed with status {response.status_code}: {response.text}")

            data = response.json()
            page_items = data.get("issues", [])
            items.extend(page_items)

            total = data.get("paging", {}).get("total", len(items))
            if not page_items or len(items) >= total:
                return items
            page += 1

    def fetch_sonarqube_findings(self, project_key: str, branch: str | None) -> list[Finding]:
        params = {"componentKeys": project_key, "issueStatuses": "OPEN,CONFIRMED"}
        if branch:
            params["branch"] = branch

        findings = []
        for raw in self._fetch_all_pages(params):
            if not is_security_relevant(raw):
                continue

            key = raw["key"]
            rule = raw.get("rule", "")
            rule_name = self._fetch_rule_name(rule) if rule else rule
            component = raw.get("component", "")
            line = raw.get("line")
            finding_type = "hotspot" if FORMER_HOTSPOT_TAG in raw.get("tags", []) else "vulnerability"
            findings.append(
                Finding(
                    key=key,
                    title=self._build_title(rule_name, component, line),
                    severity=self._map_severity(raw.get("severity")),
                    component=component,
                    line=line,
                    message=raw.get("message", ""),
                    finding_type=finding_type,
                    deep_link=f"{self.base_url}/project/issues?id={project_key}&issues={key}",
                    source_tool="sonarqube",
                )
            )
        return findings

    def fetch_sonarqube_resolutions(self, finding_keys: list[str]) -> dict[str, str]:
        """
        Look up exactly `finding_keys` via issues/search's `issues` param
        (a comma-separated key list) instead of componentKeys - these are
        specific already-known issue keys, not "everything in a project",
        and deliberately no issueStatuses filter, since a resolved/closed
        issue is exactly what this is checking for.
        """
        if not finding_keys:
            return {}

        resolutions: dict[str, str] = {}
        for raw in self._fetch_all_pages({"issues": ",".join(finding_keys)}):
            resolution = raw.get("resolution")
            if resolution in _RESOLVED_RESOLUTIONS:
                resolutions[raw["key"]] = resolution
        return resolutions
