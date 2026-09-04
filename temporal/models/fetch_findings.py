# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""Input model for fetch_findings_activity."""

from pydantic import BaseModel


class FetchFindingsInput(BaseModel):
    project_key: str
    scanner_type: str = "sonarqube"
    scanner_mode: str = "local"
    credentials: dict[str, str] = {}
    branch: str | None = None
