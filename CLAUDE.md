# Project conventions

## Data models

Use **Pydantic `BaseModel`** for every data model in this project - never
`dataclasses.dataclass` or plain classes. This includes types that cross a
Temporal workflow/activity boundary (`Finding`, `CreatedTicket`,
`TicketResult`, `SonarToJiraInput`, `ScreenshotAttachInput`) and any new
ones added later. The Pydantic-aware data converter
(`temporal/data_converter.py`) serializes `BaseModel`s across that boundary
automatically - a plain dataclass or dict does not, and needs manual
conversion, which is exactly what we removed by standardizing on Pydantic.

## Logging

Use **exactly one** logging mechanism inside the Temporal-executed pipeline:

- `workflow.logger` (from `temporalio.workflow`) inside workflow code
  (`temporal/workflows/`).
- `activity.logger` (from `temporalio.activity`) everywhere else that runs
  inside an activity - the activity functions themselves
  (`temporal/activities/`) and every module they call into
  (`scanner/client.py`, `scanner/screenshot.py`, `ticket/client.py`).

Both are Temporal's contextual loggers - they tag every line with
workflow/activity id, run id, attempt number, etc, and route to the same
place `workflow.logger`/`activity.logger` already do. Don't create a plain
`logging.getLogger(__name__)` logger in any file that runs inside an
activity or workflow.

The only exception is the two process entrypoints, which run before any
Temporal workflow/activity context exists: `receiver/app.py` (Flask, before
a workflow is started) and `temporal/worker.py` (bootstrap, before
`worker.run()`). Those use plain `logging.basicConfig`/`logging.getLogger`
because there's nothing to attach Temporal context to yet.

Do not introduce a third logging approach (e.g. `print`, a custom logger
wrapper, structlog, etc) anywhere in this project.
