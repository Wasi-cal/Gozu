# How this project works

This document explains the internals of the Sonar-to-Jira pipeline: what
happens end-to-end when a scan finishes, why each piece is built the way
it is, and the non-obvious decisions (and bugs found along the way) baked
into the current code. For setup/installation steps, see `README.md` —
this document assumes the system is already running and focuses on how
it behaves.

## One-paragraph  

SonarQube analyzes a project and fires a webhook. A Flask receiver
verifies the webhook's signature and starts a Temporal workflow. That
workflow fetches the project's open vulnerabilities and security
hotspots from SonarQube (via the generic `ScannerClient` interface),
creates a Jira ticket for each one that doesn't already have one
(deduping via a label, via the generic `TicketClient` interface), and
then — for every ticket it just created — concurrently renders a
syntax-highlighted PNG of the flagged code (server-side, via Pygments
against SonarQube's own source-lines API - no browser involved), attaches
it to the ticket, and posts the finding's own message as a comment. A
broken snippet render can never affect ticket creation or dedupe; that
isolation is the central design constraint of the whole system.

## Architecture

```
SonarQube (Docker, self-hosted Community Build or Cloud)
   |  either `gozu run` triggers a scan directly, or SonarQube's own
   |  webhook fires when analysis finishes (HMAC-SHA256 signed)
   v
cli/scan_runner/ (direct)  OR  receiver/app.py (webhook, Flask, :5000)
   |  webhook path: verify_signature.py checks the HMAC signature,
   |  starter.py connects to Temporal and starts ScanToTicketWorkflow
   v
Temporal server (:7233)
   v
temporal/worker.py -> temporal/workflows/scan_to_ticket.py (ScanToTicketWorkflow)
   |
   |--1--> temporal/activities/fetch_findings.py
   |         -> scanner/factory.py's get_scanner_client()/build_scanner_client()
   |            picks a concrete ScannerClient (scanner/sonarqube_server.py or
   |            scanner/sonarqube_cloud.py)
   |         -> hits SonarQube's REST API, classifies vulnerability vs hotspot
   |            (scanner/sonarqube_classify.py)
   |
   |--2--> temporal/activities/create_tickets.py
   |         -> ticket/factory.py's build_ticket_client()/get_ticket_client()
   |            picks a concrete TicketClient (ticket/jira_client.py)
   |         -> find_existing (dedupe) -> create_ticket
   |         -> new tickets get moved into the active sprint, if one exists
   |
   |--3--> (fanned out concurrently, once per ticket just created)
             temporal/activities/capture_and_attach_screenshot.py
               -> scanner/screenshot.py: Pygments renders a syntax-highlighted
                  PNG of the source lines around the flagged line (SonarQube's
                  /api/sources/lines + a local Pygments render, no browser)
               -> ticket/jira_client.py: attach_screenshot (idempotent) + add_comment
```

Activities 1 and 2 run sequentially with the SDK's default retry policy
and a 30s timeout each. Activity 3 runs with its own 45s timeout and
`RetryPolicy(maximum_attempts=2)` — a snippet-rendering failure itself
never even reaches this retry policy (the activity catches and logs it
internally, see below); what's left to retry here is genuinely transient
network/timing on the Jira calls the activity makes.

## Walking through one request

1. Someone runs `sonar-scanner` against this repo (or any project pointed
   at this SonarQube instance). Analysis finishes; SonarQube POSTs a
   webhook to `receiver/app.py`, signing the body with
   `SONAR_WEBHOOK_SECRET`.
2. `verify_signature.py` recomputes the HMAC and compares it
   (`hmac.compare_digest`, timing-safe) against the
   `X-Sonar-Webhook-HMAC-SHA256` header. A mismatch or missing header
   returns `401` and stops there.
3. `receiver/starter.py` connects to the Temporal server and starts
   `ScanToTicketWorkflow` with the project key and the webhook's `taskId`
   (used to build a deterministic workflow ID: `sonar-jira-{task_id}`, so
   re-delivering the same webhook doesn't start a duplicate workflow
   run). The route itself is `POST /webhooks/sonarqube/<config_name>`,
   not a bare `/webhooks/sonarqube` — the config name in the path is how
   a single receiver serves multiple saved configs (see [Managing saved
   configs](README.md#managing-saved-configs) in the README).
4. The workflow's first activity, `fetch_findings_activity`, asks
   `scanner/factory.py`'s `get_scanner_client()` (or, when the workflow
   was started with explicit per-config credentials, `build_scanner_client()`
   directly) for a `ScannerClient` — `SonarQubeServerClient` or
   `SonarQubeCloudClient` depending on scanner mode — whose
   `fetch_findings()` hits `/api/issues/search` once (`issueStatuses=OPEN,CONFIRMED`)
   and classifies each result as a vulnerability or a former security
   hotspot by inspecting its tags (`scanner/sonarqube_classify.py`'s
   `FORMER_HOTSPOT_TAG`) rather than calling a separate hotspots
   endpoint. `branch` comes from the caller (the CLI's local git checkout,
   or the webhook payload) when supplied; only if nothing supplied one
   does the activity fall back to shelling out to
   `git rev-parse --abbrev-ref HEAD` in the worker's own working
   directory — which structurally can't resolve to anything but `None` in
   the containerized worker, since the image has no `.git` (see [Why
   branch is read from local git](#why-branch-is-read-from-local-git)
   below).
5. The second activity, `create_tickets_activity`, loops over every
   finding. Dedupe is two-layered: `ticket/claims.py`'s Postgres ledger
   atomically claims a `(destination, finding_key)` pair before anything
   talks to Jira (closing races between overlapping runs), and
   `find_existing` — a JQL search for a ticket labeled
   `source-key-{finding.key}` — is the fallback for tickets that predate
   the ledger. A per-run `ticket_cap` (from the config, or a `--ticket-cap`
   override; `BACKLOG_CAP = 30` if neither is set) limits how many
   *new* tickets one run creates; anything past the cap is left
   unclaimed and rolled into one backlog rollup ticket instead of being
   created individually. For each finding that does get a new ticket,
   `create_ticket` POSTs a new Jira issue (type `Bug`, priority mapped
   from the normalized severity, labeled `source-sonarqube`, `security`,
   `type-{finding_type}`, optionally `branch-{branch}`, and
   `source-key-{finding.key}`) and, if the project has an active sprint,
   moves it there.
6. If any tickets were created, the workflow fans out
   `capture_and_attach_screenshot_activity` once per created ticket,
   concurrently, via `asyncio.gather(..., return_exceptions=True)`. Each
   invocation:
   - Posts the finding's own `message` as a Jira comment (`add_comment`) -
     independent of the snippet render below, not scraped from anywhere.
   - Fetches the source lines around `finding.line` from SonarQube's
     `/api/sources/lines` and renders them as a syntax-highlighted PNG via
     Pygments (`scanner/screenshot.py`'s `render_finding_snippet()` - see
     the deep dive below), then attaches it (`attach_screenshot`, skipping
     if a same-named file is already there).
   - A snippet-rendering failure is caught INSIDE the activity, logged
     clearly (which finding, why), and the activity returns normally
     rather than raising - it never even reaches step 7's retry policy.
   - Cleans up its temp directory in a `finally`, whether it succeeded or
     not.
7. `asyncio.gather(..., return_exceptions=True)` means a permanently
   failing Jira call (after its own 2 retry attempts) shows up as an
   exception object in the results list rather than raising — the
   workflow logs a warning per failure and still returns the
   `TicketResult` from step 5 successfully. **The screenshot/comment
   step can never turn a successful ticket-creation run into a failed
   workflow.**

## Component reference

### `receiver/` — the webhook entrypoint

- **`app.py`** — `GET /health` (for `docker-compose.yml`'s healthcheck)
  and `POST /webhooks/sonarqube/<config_name>`, the actual webhook route.
  The config name in the path is what lets one receiver serve multiple
  saved configs: it looks up that config's `webhook_secret` (404 if the
  config doesn't exist, 500 if it has no secret configured yet), verifies
  the signature against it, extracts `project.key` from the JSON body,
  starts the workflow, and returns 200 immediately — all the real work
  happens asynchronously in Temporal, so the webhook response doesn't
  wait on SonarQube API calls or Jira API calls.
- **`verify_signature.py`** — pure function, no side effects, easy to
  unit-test in isolation (though it isn't yet — see [Testing](#testing)).
- **`starter.py`** — the only piece of code that knows how to start this
  specific workflow; kept separate from `app.py` so the Flask route
  doesn't need to know about Temporal's `Client`/`data_converter`
  plumbing directly.

### `temporal/` — orchestration

- **`data_converter.py`** — one constant (`TASK_QUEUE`) and one object
  (`pydantic_data_converter`) shared by the worker, the workflow starter,
  and (implicitly) every activity. Every model in this codebase is a
  Pydantic `BaseModel` (`Finding`, `TicketResult`, `ScreenshotAttachInput`,
  ...), so this converter serializes every one of
  them across the workflow/activity boundary automatically — no manual
  `.model_dump()`/`.model_validate()`, and no dataclass-to-dict
  workarounds, anywhere.
- **`worker.py`** — connects to Temporal, registers three workflows
  (`ScanToTicketWorkflow`, `MultiBranchScanWorkflow`,
  `ReconcileOnlyWorkflow`) and four activities
  (`fetch_findings_activity`, `create_tickets_activity`,
  `capture_and_attach_screenshot_activity`,
  `reconcile_resolved_findings_activity`) as plain imported function
  objects (not by string name), and blocks on `worker.run()`. Any new
  workflow or activity must be imported and added to the
  `workflows=[...]`/`activities=[...]` lists here or it silently can't
  be scheduled (a start request would hang waiting for a worker that
  never claims the task).
- **`workflows/scan_to_ticket.py`** — `ScanToTicketWorkflow` is the
  orchestration logic walked through in detail above; it's the one every
  scan (CLI or webhook) ultimately runs. Notice what it does *not* do:
  no try/except around `execute_activity` calls for the first two
  activities. Temporal's own retry policy (default, since none is
  passed) handles transient failures there, and if
  `fetch_findings_activity` or `create_tickets_activity` fails
  permanently, the whole workflow is meant to fail loudly — there's no
  reason to hide a broken SonarQube/Jira connection. The screenshot
  activity is the only one wrapped in failure-tolerant handling, via
  `return_exceptions=True`. `MultiBranchScanWorkflow` and
  `ReconcileOnlyWorkflow` (also under `workflows/`) are registered
  alongside it — not detailed in this walkthrough yet; see
  [Known limitations](#known-limitations).
- **`models/`** — one file per activity-boundary model
  (`sonar_to_jira.py` → `SonarToJiraInput`, `screenshot_attach.py` →
  `ScreenshotAttachInput`, `fetch_findings.py` → `FetchFindingsInput`,
  `create_tickets.py` → `CreateTicketsInput`, plus
  `reconcile_resolved_findings.py`'s input model), matching this repo's
  one-small-model-per-file convention.
- **`activities/`** — see the walkthrough above; each activity is a
  single `@activity.defn async def` function with no shared state
  between them beyond what's passed as arguments.

### `core/models.py` and the scanner/ticket seam

Unlike the pre-refactor version of this codebase (a direct SonarQube ->
Jira pipeline), the current system is generic over which scanner and
which ticketing system sit behind it:

- **`core/models.py`** — `Finding` and `Severity` are the normalized
  vocabulary every adapter translates into/out of. Neither `scanner/`
  nor `ticket/` ever sees the other's tool-specific values (SonarQube's
  `BLOCKER`/`MAJOR`, Jira's issue keys) outside its own adapter. See
  [Data model reference](#data-model-reference) for every field.
- **`scanner/base.py`** — `ScannerClient` is an `ABC` with three
  abstract methods: `fetch_findings(project_key, branch=None) -> list[Finding]`
  (combining vulnerabilities and hotspots into one list is an internal
  detail of each adapter, not part of the generic contract),
  `requirements() -> ScannerRequirements` (which Docker services/host
  binaries this scanner needs, read by the CLI wizard to decide what to
  bring up), and `fetch_resolutions(finding_keys) -> dict[str, str]`
  (batch resolution-status check used by
  `temporal/activities/reconcile_resolved_findings.py` to auto-close
  tickets). `scanner/factory.py`'s `get_scanner_client()`/
  `build_scanner_client()` is the single place that picks a concrete
  class from a scanner type + mode.
  - `SonarQubeServerClient` (`scanner/sonarqube_server.py`) — talks to
    self-hosted SonarQube. Auth is HTTP Basic with the token as username
    and an empty password (`(token, "")`), which is SonarQube's
    documented convention for using a personal access token in place of
    a username/password pair on its REST API.
  - `SonarQubeCloudClient` (`scanner/sonarqube_cloud.py`) — talks to
    SonarQube Cloud (`sonarcloud.io`), scoped by an `organization` param
    required on most of its endpoints. Both concrete clients share their
    issue-fetching/classification logic via `SonarQubeIssueFetcher`
    (`scanner/sonarqube_common.py`), so the fetch/classify behavior
    described in [Walking through one request](#walking-through-one-request)
    applies to either one identically.
- **`scanner/screenshot.py`** — see the deep dive below. Not part of the
  `ScannerClient` contract — it's a SonarQube-specific bonus capability
  (source-lines API, `SONAR_TOKEN` auth), called directly by the
  screenshot activity rather than through `get_scanner_client()`.

#### The snippet-rendering story

`render_finding_snippet(finding, token, context_lines=5) -> bytes` is the
one function this module exports — a syntax-highlighted PNG of the
source lines around the flagged line, rendered entirely server-side via
[Pygments](https://pygments.org/). No browser is involved at all: an
earlier implementation drove headless Chromium via Playwright to
screenshot SonarQube's own web UI (see git history before this rewrite
if you need the old approach's own hard-won gotchas) — that whole
approach is gone, not kept alongside this one. Two things worth
understanding because they're non-obvious and were confirmed live, not
assumed, while building this:

**1. SonarQube's `/api/sources/lines` returns HTML-marked-up code, not
plain text.** Confirmed live against a real instance: the `code` field
of each returned line looks like
`<span class="k">import</span> <span class="sym-1 sym">hashlib</span>`
— SonarQube's own syntax-highlighting markup, not the raw source. Since
Pygments does its own highlighting from scratch, feeding it pre-marked-up
HTML would double-highlight garbage. `_strip_html()` (a small
`html.parser.HTMLParser` subclass) strips every tag and keeps only the
text content before anything reaches Pygments — Python's
`HTMLParser`'s default `convert_charrefs=True` also decodes entities
(`&amp;` → `&`) along the way, so no separate unescape step is needed.

**2. The highlighted line is an offset WITHIN the snippet, not the
finding's absolute file line number.** An easy off-by-one: if
`finding.line=13` and `context_lines=5`, the fetched range is lines
8–18, and the line to pass to Pygments' `ImageFormatter(hl_lines=...)`
is `13 - 8 + 1 = 6` (1-indexed within an 11-line snippet), not `13`.
`from_line` is also clamped to a minimum of `1` — SonarQube's API
itself errors (`400: "Line number must start at 1"`) on a request with
`from < 1`, which a finding near the top of a file combined with a
generous `context_lines` would otherwise produce. Confirmed live against
a real multi-function file that this offset is correct at both a
`from_line=1` (finding near the top) and a genuinely mid-file
`from_line` (a finding at line 13 in an 18-line file) — the rendered
PNG's highlighted row matches the real flagged line exactly in both
cases, not just the trivial one.

Beyond those two, the rest is a straight pipeline: fetch the line range,
strip HTML, pick a Pygments lexer via `get_lexer_for_filename()` on the
finding's own file extension (falling back to a plain `TextLexer` if
nothing matches, rather than raising), render through
`ImageFormatter(line_numbers=True, line_number_start=from_line, hl_lines=[...])`
into an in-memory buffer, and return the raw PNG bytes.

**Failure here is caught and made visible, not silent.** Unlike the
Playwright-based version's failure behavior, `render_finding_snippet()`
itself always raises on a genuine failure (a bad SonarQube response, an
unexpected shape) rather than degrading quietly - it's
`capture_and_attach_screenshot_activity` (the caller) that decides how
to handle that: catches it, logs a warning naming the specific finding
and the real reason, and continues without attaching an image rather
than a ticket silently ending up with no attachment and no explanation
either way. The `add_comment` (finding's own message) and the snippet
attachment are two independent best-effort pieces now, not one unit tied
to a single extraction call - a snippet-rendering failure never prevents
the comment from being posted.

Pygments' `ImageFormatter` needs [Pillow](https://python-pillow.org/) to
actually rasterize (`pygments` alone raises `PilNotAvailable` without
it — confirmed live) — `pyproject.toml` depends on both explicitly, not
just `pygments`.

#### Why branch is read from local git

`SonarQube Community Build` doesn't report per-issue (or per-scan)
branch information through its REST API at all — confirmed directly by
inspecting `/api/issues/search`'s raw JSON response, which has no
`branch` field anywhere. Multi-branch analysis is a Developer
Edition+ feature. Since this project only ever has one local checkout on
one machine, `fetch_findings_activity` reads
`git rev-parse --abbrev-ref HEAD` in the worker's own working directory
and stamps it onto every `Finding` before returning. This is accurate
for this specific local, single-checkout setup, but it is **not** a
substitute for real SonarQube branch-aware analysis — it reports
whichever branch happens to be checked out on the worker machine at
fetch time, which may not always be the exact branch that was actually
scanned if they ever diverge (e.g. someone switches branches on the
worker's machine between a scan finishing and the workflow's activity
running).

### `ticket/jira_client.py` — the Jira Cloud integration

- **`TicketClient`** (`ticket/base.py`) is an `ABC` with three required
  methods (`destination_id`, `find_existing`, `create_ticket`) — the
  seam between "some ticketing product" and everything upstream;
  nothing outside `ticket/jira_client.py` should ever construct
  `JiraClient` directly. `ticket/factory.py`'s `get_ticket_client()`/
  `build_ticket_client()` is the single place that picks a concrete
  class. `destination_id()` returns a stable string (e.g.
  `jira:{base_url}:{project_key}`) that scopes `ticket/claims.py`'s
  idempotency ledger, so two different destinations tracking the same
  finding key never collide.
- **`JiraClient`** — the only concrete implementation today. Every
  method follows the same shape: build a `requests` call with
  `auth=self.auth`, check the response via `_raise_for_status` (a plain
  `RuntimeError` with the status code and body — no custom exception
  hierarchy anywhere in this codebase).
  - `find_existing` / `create_ticket` — required `TicketClient` methods;
    `find_existing`'s label-based JQL search is the fallback dedupe
    layer behind `ticket/claims.py`'s Postgres ledger (see [Walking
    through one request](#walking-through-one-request)). Dedupe is a
    Jira **label** (`source-key-{finding.key}`), not a custom field,
    specifically so it works on a brand-new free-tier project with zero
    admin setup (custom fields need to be added to a project's
    screen/permission scheme before the API can use them; labels are a
    built-in field on every project).
  - `ticket_exists`, `discover_custom_fields`, `upsert_rollup_ticket` —
    further bonus, backend-specific capabilities dispatched via
    `getattr()` alongside `attach_screenshot`/`add_comment` below;
    `create_tickets_activity` uses them to verify a ledger-claimed
    ticket still exists, discover optional custom fields once per run,
    and keep one rollup ticket in sync with the current deferred
    backlog, respectively.
  - `_get_active_sprint_id` / `_add_issue_to_active_sprint` — looks up
    the project's first Agile board and its active sprint (cached
    per-`JiraClient` instance, since it can't change mid-run), and moves
    every newly-created ticket into it. No active sprint just means new
    tickets land in the backlog — logged as a warning via
    `activity.logger`, not an error.
  - `_build_description_adf` — builds the ticket description as
    Atlassian Document Format: the finding's message as a paragraph,
    then a bullet list of Component / Line / Type / Severity / Source /
    **Branch** / a link to the SonarQube deep link.
  - `attach_screenshot` — **not part of the `TicketClient` ABC** (a
    Jira-specific bonus capability, dispatched via `getattr()`, see
    below) and **idempotent by filename.** Before uploading, it GETs the
    ticket's existing attachments and skips the upload if a file with
    the same name (`{finding.source_tool}-{finding.key}.png`) is already
    present. This matters because Temporal may retry the calling
    activity — without this check, a retry after a transient network
    blip could attach the same screenshot twice.
  - `add_comment` — also not part of the `TicketClient` ABC. Takes a
    plain string and wraps the *entire* thing in a single ADF
    `codeBlock` node (no parsing of the string into separate
    paragraph/code sections — the caller is responsible for assembling
    whatever text it wants shown, in whatever order).

### The "bonus method" pattern in `capture_and_attach_screenshot_activity`

Both `attach_screenshot` and `add_comment` are invoked through
`getattr(client, "method_name", None)` rather than called directly:

```python
attach_screenshot = getattr(client, "attach_screenshot", None)
if attach_screenshot is not None:
    attach_screenshot(input.ticket_key, extraction.screenshot_path)
else:
    activity.logger.warning(...)
```

This treats both methods as *optional capabilities* a configured ticket
client may or may not support, even though today there's only one
`JiraClient` and it always supports both. The important distinction:
this `getattr` check only guards against the method **not existing at
all**. If the method exists and raises a real error (a genuine Jira API
failure), that exception is **not** caught here — it propagates out of
the activity uncaught, exactly like every other real error in this
codebase, so Temporal's `RetryPolicy(maximum_attempts=2)` on this
activity actually gets a chance to retry it. Swallowing real errors
inside the activity would silently defeat that retry policy; the only
thing the workflow tolerates as truly non-fatal is a screenshot activity
that has exhausted its own retries (handled by `asyncio.gather(...,
return_exceptions=True)` in the workflow, not by anything inside the
activity itself). The two capabilities are also checked independently of
each other, so a hypothetical future `TicketClient` that supports
`add_comment` but not `attach_screenshot` (or vice versa) still gets
whichever half it supports.

## Data model reference

Every model in this codebase is a Pydantic `BaseModel` — there are no
`dataclasses.dataclass`es anywhere (see `CLAUDE.md`).

| Model | File | Fields | Crosses a Temporal boundary? |
|---|---|---|---|
| `Finding` | `core/models.py` | `key`, `title`, `severity`, `component`, `line`, `message`, `finding_type`, `deep_link`, `source_tool`, `branch` | Yes — activity input/output |
| `CreatedTicket` | `core/models.py` | `finding_key`, `ticket_key` | Yes — nested in `TicketResult` |
| `TicketResult` | `core/models.py` | `created: list[CreatedTicket]`, `skipped: list[str]` | Yes — `create_tickets_activity`'s return type and the workflow's return type |
| `SonarToJiraInput` | `temporal/models/sonar_to_jira.py` | `project_key`, `task_id` | Yes — the workflow's input |
| `ScreenshotAttachInput` | `temporal/models/screenshot_attach.py` | `finding: Finding`, `ticket_key` | Yes — `capture_and_attach_screenshot_activity`'s input |

## Database schema & migrations

gozu's own Postgres schema (`configs`, `config_credentials`,
`ticket_destinations`, `ticket_destination_credentials`, `ticket_claims`)
is created and evolved by [Alembic](https://alembic.sqlalchemy.org/),
via `config/migrations.py`'s `run_migrations()` (Alembic's own Python
API - `alembic.command.upgrade(cfg, "head")` - not a subprocess
shell-out to the `alembic` CLI). This replaced an earlier two-path setup
where `sql/init.sql` ran once via Postgres's own
`docker-entrypoint-initdb.d` hook on a genuinely fresh volume, and
`gozu down --wipe` separately re-ran that same file by hand (that hook
never fires twice on the same volume) - `docker-compose.yml`'s
`postgres` service no longer mounts anything into
`/docker-entrypoint-initdb.d/` for gozu's own schema at all.
`run_migrations()` is now the single, explicit path both `gozu up`
(right after Postgres is confirmed reachable) and
`gozu down --wipe`'s reset (right after its scoped `DROP DATABASE`/
`CREATE DATABASE`) call - the first revision IS the fresh-install case,
applied the exact same way as every revision after it, not a
special-cased first step.

Revisions live under `migrations/versions/` (named
`NNNN_description.py`, chained via each file's `down_revision`), NOT
`alembic/versions/` - a directory literally named `alembic` at that
nesting depth was confirmed live to be silently dropped in its entirety
from a real `uv build --wheel`, almost certainly a name collision with
the installed `alembic` PyPI package during hatchling's own file
resolution; `alembic.ini`'s `script_location` (and
`config/migrations.py`'s override of it) points at `migrations`
accordingly. Every revision is pure `op.execute(<real DDL>)` -
`target_metadata = None` in `migrations/env.py`, since there's no
ORM/SQLAlchemy model layer anywhere in this codebase to diff against.
Scope is deliberately narrow: **Alembic manages schema evolution only** -
`config/store.py`, `config/ticket_destinations.py`, and
`ticket/claims.py` all keep talking to Postgres via raw `psycopg`
exactly as they already did; SQLAlchemy is present solely because
Alembic depends on it to drive a migration's own DB connection.

Each revision runs inside its own transaction (Alembic's default for a
transactional-DDL database like Postgres) - a failure rolls back that
revision and raises rather than leaving the schema half-migrated or
silently skipping ahead to the next one.

**To make a future schema change:** `alembic revision -m "description"`
(from the repo root - reads `alembic.ini`, writes a new file under
`migrations/versions/`), then hand-write its `upgrade()`/`downgrade()`
with real `op.execute()` DDL - nothing else. The next `gozu up` or
`gozu down --wipe` picks it up automatically.

**To roll back a bad revision:** `alembic downgrade -1` (from the repo
root, against whichever database `.env` points at) reverses the most
recently applied revision via its own `downgrade()` - the concrete
capability an Alembic-based approach adds over a hand-rolled
apply-only runner, confirmed live against a fully-migrated database as
part of building this.

`sql/init_sonarqube_db.sql` (creating the separate `sonarqube`
database/role local-mode SonarQube's own Postgres backend uses, in the
same instance) is deliberately NOT part of this migration history - it's
a single idempotent "ensure this exists" step with no evolving schema of
its own (SonarQube manages its own schema internally once pointed at an
empty database), so it stays on the old `docker-entrypoint-initdb.d`
hook.

## Testing

The `tests/` tree mirrors the source layout
(`tests/scanner/test_screenshot.py`, `tests/temporal/activities/test_capture_and_attach_screenshot.py`)
and needs no live SonarQube or Jira — everything is mocked with stdlib
`unittest.mock`:

- **`tests/scanner/test_screenshot.py`** mocks `requests.get` (SonarQube's
  `/api/sources/lines`) to verify: HTML markup is genuinely stripped from
  the `code` field; the `from` line is clamped to a minimum of 1; the
  highlighted-line offset within the snippet is correct both when
  `from_line=1` and when it's genuinely mid-file (the actual off-by-one
  risk); a real end-to-end render produces real PNG bytes (checked
  against the PNG file signature); an unknown file extension falls back
  to a plain-text lexer instead of raising.
- **`tests/temporal/activities/test_capture_and_attach_screenshot.py`**
  fakes the ticket client (via `get_ticket_client`) to verify: the
  comment is posted with exactly `finding.message`, independent of
  snippet rendering; both bonus methods are skipped cleanly (with a
  logged warning, no exception) when a client double doesn't have them; a
  snippet-rendering failure is caught, logged with the finding's key and
  the real reason, and does NOT fail the activity (item 9's "visible, not
  silent" requirement); a **real** error from `add_comment`/
  `attach_screenshot` themselves still propagates out of the activity;
  the temp directory is removed after a successful attach.

Run everything with:

```bash
pip install -r requirements.txt   # includes pytest + pytest-asyncio
pytest -v
```

`pytest.ini` sets `asyncio_mode = auto` so async test functions don't
need `@pytest.mark.asyncio` on each one.

## Configuration

All of it is read from environment variables, with no config file and
no `python-dotenv` — `.env` must be `source`d into the shell manually
before running the worker or receiver (see `README.md` for the exact
`set -a; source .env; set +a` incantation and why).

| Variable | Read by | Purpose |
|---|---|---|
| `SONAR_WEBHOOK_SECRET` | `receiver/app.py` | HMAC key for verifying incoming webhooks |
| `SCANNER_TYPE` | `scanner/factory.py` | `sonarqube` or `sonarqube-cloud` — picks the `ScannerClient` implementation (falls back to the legacy `SONAR_MODE` var if unset) |
| `SONAR_HOST_URL` | `scanner/factory.py`, `scanner/screenshot.py` (via `finding.deep_link`) | Base URL of the SonarQube instance |
| `SONAR_TOKEN` | `scanner/factory.py`, `scanner/screenshot.py` | User token; used as the REST API's Basic-auth username for both the main issues-search calls and `render_finding_snippet()`'s `/api/sources/lines` call |
| `SONAR_ORGANIZATION` | `scanner/factory.py` | Required when `SCANNER_TYPE=sonarqube-cloud` — passed to `SonarQubeCloudClient`, which needs it on most SonarQube Cloud API calls |
| `TICKET_BACKEND` | `ticket/factory.py` | `jira` — picks the `TicketClient` implementation |
| `JIRA_URL` | `ticket/factory.py` (via activities) | Base URL of the Jira Cloud site |
| `JIRA_EMAIL` | `ticket/factory.py` (via activities) | Account the API token belongs to |
| `JIRA_API_TOKEN` | `ticket/factory.py` (via activities) | Jira Cloud API token |
| `JIRA_PROJECT_KEY` | `ticket/factory.py` (via activities) | Which Jira project tickets are created in |

## Known limitations

- **`MultiBranchScanWorkflow` and `ReconcileOnlyWorkflow` aren't
  documented in this file yet.** Both are registered in `worker.py`
  alongside `ScanToTicketWorkflow` (see [`temporal/`](#temporal--orchestration)
  above and `temporal/activities/reconcile_resolved_findings.py`, which
  the latter presumably drives) but this document's [Walking through one
  request](#walking-through-one-request) only covers the single-branch
  `ScanToTicketWorkflow` path — a gap to close, not a claim that they
  don't exist.
- **Snippet rendering depends on `/api/sources/lines`'s response shape**
  (specifically, that `code` is HTML-marked-up text `_strip_html()` can
  parse) - a much smaller surface than the old DOM-selector approach it
  replaced, but still worth re-checking against a live instance after a
  major SonarQube upgrade.
- **`branch` is a best-effort local read, not real branch-aware
  analysis** (see above) — it reflects the worker machine's git state at
  fetch time, not necessarily the exact commit/branch that produced the
  analysis SonarQube is reporting on.
- **The "bonus method" defensive dispatch has no second implementation
  to actually exercise it.** `getattr(client, "add_comment", None)` will
  always find the method in this codebase today, since `JiraClient` is
  the only ticket client that exists — the pattern is there for when
  (if) a second backend is added, not because it currently does
  anything at runtime.

- hi this is prakrit