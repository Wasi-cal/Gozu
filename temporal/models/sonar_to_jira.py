"""Input model for SonarToJiraWorkflow."""

from pydantic import BaseModel


class SonarToJiraInput(BaseModel):
    project_key: str
    task_id: str | None = None
