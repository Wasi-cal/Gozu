# Project conventions

## The product

This repo is being built into a commercial CLI tool called **Gozu** (the
name is decided, not a placeholder - use it consistently: the command is
`gozu`, host-side dirs like `~/.gozu/`). The repo/working directory stays
`sonar-to-jira` for now - don't rename it without checking first, since
that's a separate decision from the product's own name.
It lets users scan code with SonarQube and auto-create Jira tickets for
vulnerabilities, orchestrated with Temporal. The build is happening in
phases (Phase 1: Docker Compose infra + encrypted config store. Phase 2:
`gozu init`, the setup wizard - see README.md for both); check with
the user before assuming a later phase's scope (e.g. `gozu run`,
actually running a scan) is open to work on.

## CLI conventions (Phase 2+)

- The `cli/` package holds everything CLI-specific: `main.py` (the typer
  app), `init_wizard/`, `scan_runner/`, `prerequisites/` (each a small
  package, one file per step/concern - see their own docstrings), plus
  `help_links.py` and `stack.py`.
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
`migrations/versions/0001_initial_schema.py`'s `configs` table, `config/store.py`) is called a **config**,
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
  (`scanner/`, `ticket/`).

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

## Code style: modular, reusable, no needless duplication

Favor small, single-responsibility functions over long ones, and pull out
a shared helper the moment the same shape of logic appears twice - don't
wait for a third copy. Concretely, follow the patterns already in this
codebase rather than inventing new ones:

- **Pure-builder + env-reading-wrapper split**, for anything that can be
  configured either explicitly or from the environment: `scanner/factory.py`'s
  `build_scanner_client(scanner_type, scanner_mode, credentials)` (no env
  reads) vs `get_scanner_client()` (reads env vars, delegates to the
  builder); `ticket/factory.py`'s `build_ticket_client()`/`get_ticket_client()`
  mirror it. When adding a new backend or a new per-config code path, add
  to the pure builder and let the env-reading wrapper stay a thin
  translation layer - don't duplicate the branching in both.
- **Extract shared download/verify logic**, not per-tool copies: see
  `cli/prerequisites/archive.py`'s `download_archive()`/`extract_archive()`/
  `make_tree_executable()`/`verify_runnable()`, shared by `java.py`'s
  `ensure_java()` and `sonar_scanner.py`'s `ensure_sonar_scanner()`. A third
  `ensure_*()` for a new host dependency should reuse these, not reimplement
  its own download loop. Same idea for small derived values used in more
  than one place - e.g. `cli/scan_runner/config_fields.py`'s
  `scanner_host_url(config)`, computed once and reused rather than
  recomputed per call site.
- **Dedicated, narrow Pydantic models per activity boundary** (interface
  segregation) rather than one bloated shared input model: `fetch_findings_activity`
  takes `FetchFindingsInput`, `create_tickets_activity` takes
  `CreateTicketsInput`, not the full `SonarToJiraInput` - each activity
  only sees the fields it actually needs.

This isn't a call for premature abstraction - three near-identical lines
inline are still fine. It's specifically about not letting the same
piece of logic (a download-and-verify flow, a client-construction
branch, a derived value) exist in more than one place at a time.

## File headers: copyright/author on every file, every machine

Every source file in this repo carries a header, added retroactively in
one pass and required on every new file since - this must happen the same
way regardless of who's working or which machine, not something to
remember case-by-case:

```
# Copyright (c) 2026 Calfus Inc.
# Author: <original author's real name>
# Editor: <anyone else who has since modified this file, comma-separated>
```

No license line - ownership is still being confirmed with the company;
don't add one until that's resolved. Comment syntax matches the file
(`#` for Python/YAML/TOML/Dockerfile/plain config files, `--` for
`.sql` files, `//` for anything C-style). Place it after a
shebang line where one exists (the shebang must stay the very first
line), before everything else - including a module docstring - otherwise.

- **Author** is whoever actually wrote the file first - for a brand new
  file being created in this session, that's the user driving the
  session, by their real name (check the git identity/context for this
  repo rather than guessing or using "Claude"). Never invent a name.
- **Editor** is anyone else who has since modified that same file -
  omit the line entirely if there isn't one yet. When someone other than
  the original author changes an existing file, add their name to the
  Editor line if it isn't already there (comma-separate multiple
  editors); don't touch Author once it's set. Same rule as Author:
  real people only, by their real name (check the git identity/context
  for this repo) - never "Claude", even when Claude is the one literally
  typing the edit. Claude is a tool the person is using, not a credited
  contributor. If the resulting editor would be the same person as
  Author, don't add an Editor line at all - that's already covered by
  Author.
- **Dependency line** (optional, added under a blank `#`): a factual,
  one-line "Depends on: X (Vendor) - what for" note, ONLY for a file that
  directly talks to one of the four things this product's identity
  depends on - SonarQube (SonarSource), Jira (Atlassian), Temporal
  (Temporal Technologies), or PostgreSQL - and only when that file is
  actually doing the talking (a direct API/SDK call), not just adjacent
  to one. No language implying partnership, endorsement, or certification
  by the third party - state the dependency, nothing more. Most files get
  no dependency line at all; don't force one in where it doesn't apply.
- **In-house dependency line(s)** (same block, one line per dependency):
  a file with a genuine load-bearing dependency on another module
  *within this repo* also gets its own "Depends on: <repo-relative file
  path> - what for" line - e.g. "Depends on: config/store.py - reads/
  writes the configs table". Repo-relative file path, not an import
  statement or module dotted-path. Same bar as the vendor line above:
  only the specific module(s) a file is actually built on top of to do
  its own job (what it calls into), not every module it happens to
  import - a shared status/glyph/constants helper doesn't count. One
  line per dependency when there's more than one. Most files still get
  none.
- Markdown files (`README.md`, `ARCHITECTURE.md`, this file) are
  deliberately excluded - no comment syntax was ever specified for them,
  and guessing one (e.g. an HTML comment block) isn't this convention's
  call to make unilaterally. Generated/vendored files (e.g. `uv.lock`)
  are excluded too - never hand-edit those anyway.
- Skip this convention only if the user explicitly asks to - never
  silently omit a header because a file feels too small or too generated
  to bother.
