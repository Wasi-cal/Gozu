# Project conventions

## The product

This repo is being built into a commercial CLI tool - working name
`codescan` (placeholder; use it consistently: the command is `codescan`,
host-side dirs like `~/.codescan/`). The repo/working directory stays
`sonar-to-jira` until a real name is picked - don't rename it preemptively.
It lets users scan code with SonarQube and auto-create Jira tickets for
vulnerabilities, orchestrated with Temporal. The build is happening in
phases (Phase 1: Docker Compose infra + encrypted config store. Phase 2:
`codescan init`, the setup wizard - see README.md for both); check with
the user before assuming a later phase's scope (e.g. `codescan run`,
actually running a scan) is open to work on.

## CLI conventions (Phase 2+)

- The `cli/` package holds everything CLI-specific: `main.py` (the typer
  app), `init_wizard.py`, `prerequisites.py`, `help_links.py`.
- Every interactive select/confirm prompt uses **questionary** (arrow-key
  menus), not typed option strings or plain `input()` - this is a fixed
  choice for this project, not a per-prompt toss-up, given how many
  branching selects the wizard has.
- Packaging is **uv**, not pip/`requirements.txt` (removed). Dependencies
  live in `pyproject.toml`'s `[project.dependencies]`, pinned via
  `uv.lock`. Install with `uv sync`; run things with `uv run <cmd>` or an
  activated `.venv`. The worker `Dockerfile` builds via `uv sync --frozen`
  against the same `pyproject.toml`/`uv.lock`, not a separate
  requirements file - keep it that way rather than hand-maintaining two
  dependency lists.
- A credential/doc-link helper (`cli/help_links.py`'s `HELP_LINKS`) must
  use real, current URLs looked up live (WebSearch or equivalent) - never
  a plausible-looking guessed URL.

## Naming: "configs", not "profiles"

A saved, named set of scanner + ticket-backend credentials (see
`sql/init.sql`'s `configs` table, `config_store.py`) is called a **config**,
never a "profile". Docker Compose already has an unrelated built-in concept
called profiles (`docker-compose.yml`'s `profiles: [...]` on the
`sonarqube` service, for conditionally starting services) - reusing "profile"
for credential sets would be confusing throughout the codebase and docs
given both concepts exist side by side in this project.

## Data models

Use **Pydantic `BaseModel`** for every data model in this project - never
`dataclasses.dataclass` or plain classes. This includes types that cross a
Temporal workflow/activity boundary (`Finding`, `CreatedTicket`,
`TicketResult`, `SonarToJiraInput`, `ScreenshotAttachInput`) and any new
ones added later (e.g. `ScannerRequirements`, `FindingExtraction`). The
Pydantic-aware data converter (`temporal/data_converter.py`) serializes
`BaseModel`s across that boundary automatically - a plain dataclass or
dict does not, and needs manual conversion, which is exactly what we
removed by standardizing on Pydantic.

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
