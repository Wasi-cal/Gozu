"""Shared Temporal configuration: task queue name and the Pydantic-aware data converter."""

from temporalio.contrib.pydantic import pydantic_data_converter

TASK_QUEUE = "sonar-jira-queue"
DATA_CONVERTER = pydantic_data_converter
