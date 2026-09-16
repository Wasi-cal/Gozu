# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: Temporal (Temporal Technologies) - workflow orchestration

"""Shared Temporal configuration: task queue name and the Pydantic-aware data converter."""

from temporalio.contrib.pydantic import pydantic_data_converter

TASK_QUEUE = "gozu-task-queue"
DATA_CONVERTER = pydantic_data_converter
