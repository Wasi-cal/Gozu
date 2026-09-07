# gozu — the complete guide

This document explains **everything** about this codebase: what the product
is, exactly what happens when you use it, how every module works, how the
GitHub Actions pipelines fit in, and where the existing docs (`README.md`,
`ARCHITECTURE.md`, `CLAUDE.md`) are stale relative to the current code. It's
written to stand alone — you shouldn't need to open the source to follow it.

> **A note on the other docs in this repo**: `ARCHITECTURE.md` and
> `.env.example` describe an **earlier phase** of this project, before it
> became a multi-config CLI tool. They still say things like "everything is
> read from environment variables, no config file" and describe
> `scanner/client.py`/`ticket/client.py` as single files. That's no longer
> true. `CLAUDE.md`'s own CLI-conventions bullet still lists `stack.py` as a
> single file too, which is also now stale — it's a package (§7). This
> document reflects the **current** code as of the latest pull from `main`
> (`pyproject.toml` version `0.2.3`).

---

## 1. What this actually is

**gozu** (package name in `pyproject.toml`, command `gozu`; repo/folder is
still called `sonar-to-jira`/`SonarQube-to-Jira`) is a CLI tool that:

1. Scans a codebase with **SonarQube** (self-hosted "Server"/Community
   Build, or SonarQube **Cloud**).
2. Automatically opens a **Jira** ticket for every open vulnerability /
   security hotspot that doesn't already have one.
3. Attaches a **screenshot** of the flagged code (from SonarQube's own web
   UI) plus the extracted code snippet and SonarQube's annotation text, as
   a comment on the ticket.

The whole pipeline is orchestrated by **Temporal** (a workflow engine), so
that scanning → deduping → ticket-creation → screenshotting survives
crashes and retries instead of being one long fragile Python script.

Everything runs locally via **Docker Compose**: Postgres (stores your saved
scanner/Jira credentials, encrypted), Temporal (the workflow engine), and
optionally a local SonarQube container. Jira is the only piece that must be
a real external service (Cloud or self-hosted).

---

## 2. The three ways a scan can happen

You choose one of these **per saved config**, during `gozu init`:

| Mode | Used for | Mechanics |
|---|---|---|
| `direct` | Self-hosted SonarQube, on-demand | `gozu run` shells out to `sonar-scanner` itself, waits for SonarQube's server-side analysis to finish, then triggers the ticket-creation workflow. |
| `watch` | SonarQube Cloud **Free** plan | Free only re-analyzes a PR *after* it merges to main — there's no webhook to receive on that plan, so `gozu run --watch` just re-runs the `direct` cycle on a timer (default every 300s). |
| `webhook` | Self-hosted SonarQube, or SonarQube Cloud **Premium** | SonarQube itself calls back into this tool's Flask receiver the moment its analysis finishes. Premium can track several branches (with glob patterns like `release/*`); each incoming webhook is checked against that list. |

A config tracking more than one branch and run via `gozu run` directly (not
webhook) fans out into **one child Temporal workflow per branch** under a
parent workflow (`MultiBranchScanWorkflow`) — visible in Temporal's UI as a
parent with children. A webhook delivery, by contrast, only ever concerns
one branch (SonarQube already sends one webhook per branch), so the
receiver never needs to fan out — the branch list is purely a filter on
which deliveries get processed. **Branch tagging itself is Premium-only**:
self-hosted Community Build rejects the `sonar.branch.name` scanner
property outright (a paid Developer Edition+ feature), and Cloud's Free
plan only ever analyzes `main` — so `run_scan_cycle` only computes a real
branch (`detect_git_branch(path)`) when `config["sonar_plan"] ==
"premium"`; every other case scans untagged.

---

## 3. Full walkthrough: `gozu run` (direct mode)

This is the "run a scan right now" path (`cli/scan_runner/`):

1. **`gozu run [--config NAME] [--watch] [--interval N] [--path .]`**
   (`cli/main.py`) loads `.env` into the process first (`_load_env()`, a
   `@app.callback()` that runs before every command) — `config.store` etc.
   read plain `os.environ`, and a bare `gozu` invocation can't assume your
   shell sourced `.env`.
2. `select_config(name)` (`cli/scan_runner/config_select.py`) resolves
   which saved config to use: exact match if `--config` was given, a
   `questionary` picker if there's more than one config and none was
   specified, auto-pick if there's exactly one, or a clean error if there
   are none.
3. `run_scan_cycle(config, path)` (`cli/scan_runner/__init__.py`):
   1. `ensure_java()` / `ensure_sonar_scanner()` — see §7.4; downloads
      portable copies into `~/.gozu/` the first time, verifies an existing
      one otherwise.
   2. `branch = detect_git_branch(path) if config.get("sonar_plan") ==
      "premium" else None` — see the Premium-only note in §2.
   3. `run_scanner(config, path, branch)` — builds and runs the actual
      `sonar-scanner` CLI subprocess (`-Dsonar.host.url`, `-Dsonar.token`,
      `-Dsonar.projectKey`, `-Dsonar.sources`, plus `-Dsonar.organization`
      and, only if `branch` is truthy, `-Dsonar.branch.name` for Cloud).
   4. `read_ce_task_id(path)` — sonar-scanner writes
      `.scannerwork/report-task.txt` with a `ceTaskId=...` line. The
      scanner process finishing only means the client-side **upload**
      finished — SonarQube's actual analysis happens async, server-side.
   5. `wait_for_analysis(scanner_host_url(config), config["credentials"]["sonar_token"], ce_task_id)`
      — polls `GET /api/ce/task?id=...` every 2s (up to 300s) until that
      task hits a terminal status (`SUCCESS`/`FAILED`/`CANCELED`).
   6. `trigger_workflow(config, ce_task_id, branch)`
      (`cli/scan_runner/workflow_trigger.py`) connects to Temporal on
      **`localhost:{TEMPORAL_PORT}`** (host-visible address — the CLI runs
      outside Docker, unlike the worker/receiver containers which use the
      internal `temporal:7233` hostname), builds a `SonarToJiraInput` with
      the config's actual scanner/ticket type, mode, and **decrypted
      credentials**, and starts either:
      - `ScanToTicketWorkflow` directly (single branch), or
      - `MultiBranchScanWorkflow` (if the config tracks more than one
        branch), which fans children out concurrently and aggregates their
        results.

      Workflow id is deterministic: `sonar-jira-{ce_task_id}` — re-running
      against the same SonarQube analysis task can't double-start a
      workflow.
   7. The CLI **awaits the workflow's completion synchronously**
      (`client.execute_workflow(...)`, not `start_workflow`) and returns a
      summary dict (`ce_task_id`, `created`, `skipped`), which
      `cli/main.py`'s `_print_summary()` prints — one line per created
      ticket (`created {key} for finding {key}`), one per skipped finding.

With `--watch [--interval N]` (default 300s), this whole cycle just loops
on `time.sleep(interval)` until `Ctrl+C` (caught cleanly, prints
"Stopping.").

---

## 4. Full walkthrough: the webhook path

This is what happens when SonarQube itself calls back (self-hosted, or
Cloud Premium):

1. SonarQube's analysis finishes; SonarQube POSTs to
   `receiver/app.py`'s `POST /webhooks/sonarqube/<config_name>`, signing
   the body with that config's own `webhook_secret`
   (`X-Sonar-Webhook-HMAC-SHA256` header).
2. `config = config_store.get_config(config_name)` — 404 if that named
   config doesn't exist in Postgres.
3. `verify_signature(raw_body, header, config's webhook_secret)`
   (`receiver/verify_signature.py`) recomputes the HMAC-SHA256 and compares
   it with `hmac.compare_digest` (timing-safe) — 401 on mismatch or missing
   header.
4. If `payload["status"] != "SUCCESS"`, the webhook is ignored with a 200
   (an in-progress or failed analysis isn't something to act on).
5. `payload["project"]["key"]` must match the config's own `project_key` —
   400 otherwise (guards against a webhook accidentally pointed at the
   wrong saved config).
6. `payload["taskId"]` must be present — 400 otherwise.
7. **Branch gating**: `branch = payload["branch"]["name"]`. If the config
   has a non-empty `branches` list, it's split into comma-separated glob
   patterns (e.g. `release/*`) and checked with `fnmatch.fnmatch` — if the
   incoming branch matches none of them, the webhook is a soft no-op (200,
   `"ignored"`). An empty/null `branches` field means "no restriction, take
   every branch". Note this is purely a **filter**, not a fan-out — unlike
   direct invocation's `MultiBranchScanWorkflow`, SonarQube already
   delivers one webhook per branch here.
8. `start_scan_to_ticket_workflow(config, task_id, branch)`
   (`receiver/starter.py`) connects to Temporal (`TEMPORAL_HOST` env,
   `temporal:7233` inside Docker Compose's network) and calls
   `client.start_workflow(...)` — **fire-and-forget**, it doesn't await
   completion, just workflow acceptance. Workflow id:
   `sonar-jira-{task_id}` (same deterministic scheme as the direct path).
9. Returns 200 immediately. No SonarQube compute-engine polling happens
   here at all — unlike direct invocation, the webhook firing *is*
   SonarQube telling you its server-side processing already finished.

---

## 5. What the Temporal workflow actually does (`ScanToTicketWorkflow`)

This is the shared core, run by both trigger paths
(`temporal/workflows/scan_to_ticket.py`):

1. **`fetch_findings_activity`** — asks the right `ScannerClient` (built
   from the config's `scanner_type`/`scanner_mode`/credentials, or falling
   back to legacy env vars if none were passed) to hit SonarQube's REST API
   and return normalized `Finding` objects. Retried generously (up to 8
   attempts, 2s initial backoff) because SonarQube's search index can lag
   a few seconds behind a compute-engine task's own `SUCCESS` — a genuinely
   missing project and "not indexed yet" look identical (`404 Project not
   found`), so extra retries give real indexing lag room to resolve. Every
   returned finding gets its `.branch` field stamped (from the CLI's
   detected git branch, the webhook payload's branch, or — legacy path
   only — `git rev-parse` run inside the worker container itself, which
   has no real `.git` and is documented as a vestigial fallback).
2. **`create_tickets_activity`** — for each finding, uses the idempotency
   ledger (§8) to decide whether to create a ticket, skip it, or wait —
   creates a Jira `Bug` issue (priority mapped from severity, labeled
   `sonarqube`, `security`, `source-key-{finding.key}`), and moves it into
   the project's active sprint if one exists. Retried up to 3 attempts.
3. **If any tickets were created**, fans out one
   **`capture_and_attach_screenshot_activity`** per new ticket,
   concurrently (`asyncio.gather(..., return_exceptions=True)`, 45s
   timeout, max 2 attempts each). A screenshot that fails even after its
   own retries shows up as an exception object in the results list and is
   just logged as a warning — **it can never fail the workflow or affect
   the returned ticket-creation result.** That isolation is a deliberate,
   central design constraint: a flaky headless-browser step should never
   be able to break the actually-important part (ticket creation +
   dedupe).
4. Returns a `TicketResult` (`created: [...]`, `skipped: [...]`) — this is
   exactly the dict the CLI or the webhook (indirectly, via the workflow's
   completion, though the webhook path doesn't wait for it) sees.

`MultiBranchScanWorkflow` (only used by direct invocation on a
multi-branch config) is a thin wrapper: it fans out one child
`ScanToTicketWorkflow` per branch concurrently (child workflow id
`{parent_id}-{branch}`, slashes replaced with dashes) and merges their
`created`/`skipped` lists. On SonarQube Cloud, each branch genuinely has
distinct findings. On self-hosted Community Build (which has no real
per-branch data), every child ends up querying the *same* underlying
findings — so it's the idempotency ledger (§8), not the fan-out logic
itself, that stops that from creating duplicate tickets.

---

## 6. The screenshot/extraction step, in detail

`scanner/screenshot.py`'s `capture_finding_screenshot()` is the most
fiddly piece of code in the repo, because it drives a real headless
browser against SonarQube's own web UI (there's no API for "give me a
screenshot of this issue"). Three non-obvious things had to be worked out
by trial and error against a live instance:

1. **Playwright's async API is required, not a style choice.** A Temporal
   activity is an `async def` coroutine running directly on the worker's
   event loop. Playwright's *sync* API detects an already-running event
   loop in the calling thread and refuses to even start — so this is the
   one file in the codebase that can't follow the "just call blocking I/O
   from `async def`" pattern used everywhere else (plain `requests` calls
   elsewhere block the loop, which is fine at this request volume;
   Playwright's sync API genuinely cannot be invoked here at all).
2. **`http_credentials` silently never authenticates.** Playwright's
   "correct-looking" `new_context(http_credentials=...)` option only
   attaches an `Authorization` header *after* the server responds with a
   `401` challenge. SonarQube's React SPA always returns `200` for its
   shell (then redirects client-side if you're not logged in), so that
   challenge never happens and the header is never sent — screenshots
   "succeeded" every time while silently capturing the login page. The fix
   sets the header unconditionally via
   `extra_http_headers={"Authorization": f"Basic {token}"}`, which
   Playwright sends on every request regardless of any challenge.
3. **A short viewport clips the source table.** SonarQube's source-code
   table doesn't fully render at a normal viewport height until scrolled.
   The fix is a generously tall shared viewport (`1280×2000`) set once per
   browser context, so nothing needs scrolling to be captured.

With those fixed, the actual flow: one `Browser` is launched once per
worker process (module-level singleton); one `BrowserContext` is cached
**per distinct SonarQube token** (a context's headers are fixed at
creation, and different configs can use different tokens). For a given
finding, it navigates to the finding's SonarQube deep link (with
`&open={finding.key}` appended so the source viewer auto-expands to that
specific issue), tries a short list of CSS selectors in order (`table`,
`[data-testid="source-viewer"]`, `.source-viewer`) until one becomes
visible, screenshots that element, then — from the *same* page load, no
second navigation — extracts:
- `code_snippet`: `.inner_text()` on that same matched element.
- `annotation_text`: scoped to the whole page, **not** the matched
  element — live DOM inspection found the inline issue-annotation callout
  isn't a descendant of the source table at all. It lives in a separate
  `<header>` element elsewhere on the page; since the top nav bar is also
  a `<header>`, the code picks the **last** one, whose first line of text
  is SonarQube's actual issue message.

If no selector matches at all, it falls back to a full-page screenshot
with no text extraction rather than hard-failing. Any exception during
text extraction specifically (not the screenshot itself) is caught and
logged — a broken extraction never prevents the screenshot from being
captured and attached.

This selector-guessing is inherently coupled to SonarQube's current
frontend markup (hashed CSS-in-JS class names, no stable `data-testid` for
either element in this version) — a SonarQube upgrade that changes the DOM
may require re-discovering these selectors against a live instance again.

---

## 7. Repository layout and what every piece does

```
cli/                       the `gozu` CLI (Typer)
  main.py                    entrypoint: init / run / up / down commands
  init_wizard/               `gozu init` — one file per wizard step
  scan_runner/                `gozu run` — scan, poll, trigger workflow
  prerequisites/              download/verify Java + sonar-scanner into ~/.gozu/
  help_links.py               live doc-link lookups shown during prompts
  stack/                      `gozu up`/`down [--wipe]` — package, not a file (see §7.6)

config/                    saved scanner+ticket credential sets ("configs")
  connection.py               shared get_connection() (store.py + ticket_destinations.py)
  store.py                    CRUD against Postgres
  crypto.py                   Fernet encryption for every stored secret
  ticket_destinations.py      shared Jira-board records multiple configs can reference

core/models.py             shared, tool-agnostic domain models
  Finding, Severity, CreatedTicket, TicketResult

scanner/                   scanner backends
  base.py                    ScannerClient ABC, SCANNER_REGISTRY, severity map
  sonarqube_server.py         self-hosted SonarQube implementation
  sonarqube_cloud.py          SonarQube Cloud implementation
  sonarqube_common.py         shared REST-fetching/pagination logic (mixin)
  sonarqube_classify.py       "is this issue security-relevant" logic
  factory.py                  build_scanner_client() / get_scanner_client()
  screenshot.py                Playwright screenshot + text extraction

ticket/                    ticket backends
  base.py                    TicketClient ABC, priority map
  jira_client.py               Jira Cloud/Server implementation
  jira_sprint.py               active-sprint lookup/assignment
  adf.py                       Atlassian Document Format node builders
  claims.py                    Postgres idempotency ledger (dedupe races)
  factory.py                   build_ticket_client() / get_ticket_client()

temporal/                  orchestration
  worker.py                    worker process entrypoint
  data_converter.py             shared task queue name + Pydantic converter
  workflows/
    scan_to_ticket.py            ScanToTicketWorkflow — the core pipeline
    multi_branch_scan.py         MultiBranchScanWorkflow — per-branch fan-out
  activities/                   fetch findings / create tickets / screenshot
  models/                        one Pydantic model per activity boundary

receiver/                  Flask webhook receiver
  app.py                       routes: /health, /webhooks/sonarqube/<config>
  verify_signature.py           HMAC-SHA256 verification
  starter.py                    starts a workflow on a webhook's behalf

scripts/                   one-off/bootstrap scripts
  paths.py                     GOZU_HOME / STACK_DIR — the one shared state dir
  bootstrap_env.py              writes/fills in .env (ports, keys, secrets)
  env_ports.py                   port probing + .env parsing primitives
  fernet_safety.py               refuses to silently rotate FERNET_KEY
  seed_test_config.py            manual round-trip check of config storage

sql/init.sql                Postgres schema (configs, config_credentials,
                              ticket_claims, ticket_destinations, ticket_destination_credentials)
tests/                       pytest suite (see §12 — very partial coverage)
```

### 7.1 CLI (`cli/`)

- **`gozu init`** (`cli/init_wizard/`, see §7.2) provisions `.env`,
  materializes the Docker stack files, brings up just Postgres, walks
  through scanner + ticket-destination setup, and saves it all as a named
  **config** row.
- **`gozu run`** — covered fully in §3.
- **`gozu up`** / **`gozu down [--wipe]`** — covered fully in §7.6.
- **Prerequisites** (`cli/prerequisites/`) — Java and `sonar-scanner` are
  never assumed to be installed system-wide. `ensure_java()` checks for a
  previously-downloaded copy under `~/.gozu/jre/`, then the system `PATH`,
  and only then downloads a portable Eclipse Temurin JRE (LTS 21) from
  Adoptium's API, extracted and made executable. `ensure_sonar_scanner()`
  follows the identical pattern against a pinned sonar-scanner-cli version
  (Sonar's binary bucket has no "latest" alias to poll). Both share
  download/extract/chmod/verify helpers in `archive.py`. **Java is only
  downloaded when the wizard's scanner mode is Local** (see §7.2) — asking
  unconditionally used to download a JRE even for configs that would never
  touch a local scanner.

### 7.2 The init wizard (`cli/init_wizard/`)

`run_init_wizard()` (`__init__.py`) drives the flow, in this exact order:

1. `step_bootstrap_env()` — `.env` setup (ports, `FERNET_KEY`, Postgres
   password), unchanged in shape from before.
2. `stack_dir = ensure_stack_files()` (`cli/stack/files.py`, §7.6) —
   materializes the Docker stack files into `~/.gozu/stack/`.
3. `ensure_postgres_up(stack_dir)` — brings up **just** the `postgres`
   Compose service, because `create_config()` later in this same flow
   needs a live database connection, and that can't wait for a full
   `gozu up`.
4. Scanner selection: `_select_scanner()` reads `SCANNER_REGISTRY`
   (auto-picks if there's only one entry), then `select_scanner_mode()`
   asks Local vs Cloud.
5. **`if scanner_mode == "local": step_ensure_java()`** — Java is now
   downloaded only for Local mode, deliberately placed *after* the
   Local/Cloud choice (this was previously unconditional).
6. Branches on mode: `local` → `collect_local_sonar(stack_dir)` (now takes
   `stack_dir` — see the SonarQube-startup logic below) returning
   `(credentials, trigger_mode, project_key)`; `cloud` →
   `collect_cloud_sonar()` returning `(credentials, trigger_mode,
   project_key, sonar_plan, branches)`.
7. `ticket_destination_id = collect_jira()` (`jira_step.py`) — see the
   shared-ticket-destinations flow below. Returns an **int id**, not a raw
   credentials dict.
8. Config naming (`_prompt_config_name()`, unchanged) and
   `config_store.create_config(name=..., scanner_type=..., scanner_mode=...,
   ticket_backend="jira", trigger_mode=..., project_key=..., credentials=...,
   sonar_plan=..., branches=..., ticket_destination_id=...)`.
9. `destination = config_store.get_ticket_destination_by_id(ticket_destination_id)`,
   then `print_summary(..., destination_name=destination["name"])`.

**SonarQube-before-token ordering** (`sonar_local.py`): the old flow asked
for a SonarQube token before confirming the server was even reachable.
Now `collect_local_sonar(stack_dir)` loops
`prompt_text("SonarQube host URL:")` → `_ensure_local_sonarqube(host_url,
default_host, stack_dir)` until that returns `True`, and only then prompts
for the token. `_ensure_local_sonarqube()`'s exact logic:
1. Check `_sonarqube_status(host_url)` (`GET /api/system/status`).
2. `"UP"` → confirm "already running — use it?" (default yes).
3. Some other status → confirm "doesn't look healthy — use it anyway?"
   (default no).
4. Nothing responds and the URL isn't gozu's own managed default → warn
   that gozu only manages its own instance, return `False` (caller
   re-prompts for a different URL).
5. Nothing responds and it **is** gozu's own default URL → **starts it**
   via `ensure_service_up(stack_dir, "sonarqube", profile="sonarqube-local")`.

**Shared ticket destinations** (`jira_step.py`) — this is the biggest
functional addition in this pull. A config's Jira credentials can now live
in one of two shapes:

- **Embedded** (legacy) — Jira URL/email/token/project key sit directly in
  that config's own `config_credentials` rows, exactly as before.
- **A shared ticket destination** (new default path) — the config just
  stores a `ticket_destination_id` pointing at a row in the new
  `ticket_destinations` table; any number of configs can point at the same
  row, so multiple scanner configs land tickets on the same Jira board
  without re-entering credentials.

`collect_jira() -> int`: if `config_store.list_ticket_destinations()` is
empty, goes straight to `_create_new_destination()` (prompts
`jira_url`/`jira_email`/`jira_api_token`/`jira_project_key`, a destination
name, calls `config_store.create_ticket_destination(...)`, returns the new
id). Otherwise shows a `questionary.select` listing every existing
destination by name plus a "Create a new ticket destination" choice —
picking an existing one skips straight to naming the config, with **zero**
Jira prompts.

### 7.3 Config storage (`config/`)

A **config** — never called a "profile" (Docker Compose already has an
unrelated built-in `profiles` concept) — is a named bundle of scanner +
trigger-mode settings, referencing either embedded Jira credentials or a
shared **ticket destination**. `configs` holds only the fields every
backend shares (scanner type/mode, ticket backend, trigger mode, project
key, sonar plan, tracked branches, and now `ticket_destination_id`);
everything backend-specific (host URLs, tokens, webhook secrets) lives in
`config_credentials` as arbitrary encrypted key/value pairs. Every value
is encrypted with **Fernet** (`config/crypto.py`, AES-128-CBC + HMAC) keyed
by a single `FERNET_KEY` env var — losing that key makes every stored
credential permanently unrecoverable, by design (there's no recovery path,
so `scripts/fernet_safety.py` actively **refuses** to let
`bootstrap_env.py` silently generate a *new* key if configs already exist
in a reachable Postgres, unless you explicitly pass `--force-new-key` and
type `yes` at a confirmation prompt).

**`config/connection.py`** (new) centralizes the `psycopg` connection
logic — `get_connection()` — used by both `config/store.py` and
`config/ticket_destinations.py`. **This centralization is only partial**:
`ticket/claims.py` still has its own private, duplicate `_get_connection()`
with the identical shape; it was not migrated to the shared helper.

**`config/ticket_destinations.py`** (new) backs the shared-board feature:
`create_ticket_destination(name, ticket_backend, project_key, credentials)
-> int`, `get_ticket_destination_by_id(id)` / `get_ticket_destination(name)
-> dict | None` (each merging in decrypted credentials the same way
`config/store.py`'s `get_config()` does), and `list_ticket_destinations()`
for picker UIs (no secrets, mirrors `list_configs()`).

**`config/store.py`**'s `get_config(name)` now transparently resolves
either shape: it reads `config_credentials` as always, then — if the
config row's `ticket_destination_id` is set — fetches that destination and
merges `credentials["jira_project_key"]` plus every destination credential
**on top of** whatever was already read. So a config using the shared
shape and a legacy embedded-credentials config produce the exact same flat
`credentials` dict shape by the time `ticket/factory.py` sees it — callers
never need to know which path resolved it. `store.py` also re-exports
`config/ticket_destinations.py`'s functions and `get_connection` through
its own `__all__`, acting as one façade even though the logic lives in
separate files.

### 7.4 Scanner backends (`scanner/`)

`ScannerClient` is an abstract interface (`base.py`) with one real method,
`fetch_findings(project_key, branch=None) -> list[Finding]`.
`SCANNER_REGISTRY` is the single source of truth for which scanners exist
(currently just `{"sonarqube": "SonarQube"}`) — the init wizard reads it
rather than hardcoding a list. `sonarqube_server.py` and
`sonarqube_cloud.py` are both thin wrappers around a shared
`SonarQubeIssueFetcher` mixin (`sonarqube_common.py`) that does the actual
paginated `/api/issues/search` fetching, rule-name lookups, and severity
mapping; they differ only in base URL handling (self-hosted rewrites
`localhost` to `host.docker.internal` for the worker container's own
outbound requests — but never for the finding's public-facing
`deep_link`) and Cloud's required `organization` query param.

`sonarqube_classify.py` documents an important, non-obvious fact: as of
mid-2026 SonarSource deprecated the separate hotspots API and folded
hotspots into regular issues, tagged `former-hotspot`. Since there's no
reliable way to detect which UI mode (Standard vs "MQR") a given instance
is running, `is_security_relevant()` ORs three independent signals
(`type == VULNERABILITY`, any impact with `softwareQuality == SECURITY`, or
the `former-hotspot` tag) rather than trusting any single field.

### 7.5 Ticket backends (`ticket/`)

`JiraClient` (`jira_client.py`) is the only implementation. It dedupes via
a **Jira label** (`source-key-{finding.key}`), not a custom field —
deliberately, so it works on a brand-new free-tier project with zero admin
setup. New tickets get moved into the project's active sprint if one
exists (`jira_sprint.py`, cached per-client instance) — no active sprint
just means the ticket stays in the backlog, logged as a warning, not an
error. `attach_screenshot` and `add_comment` are deliberately **not** part
of the `TicketClient` interface — they're optional "bonus" capabilities,
dispatched via `getattr(client, name, None)` at the call site rather than
called directly, so a hypothetical future ticket backend that only
supports one of them still gets whichever half it supports.
`attach_screenshot` is idempotent by filename (skips the upload if a
same-named attachment already exists) specifically because Temporal may
retry the calling activity.

**The idempotency ledger (`ticket/claims.py`)** is the piece that actually
prevents duplicate tickets under concurrency (multi-branch fan-out hitting
the same underlying SonarQube project, an activity retry, or two configs
— now more likely than ever, given shared ticket destinations — pointed at
the same Jira project). It's a small Postgres table (`ticket_claims`,
unencrypted — nothing in it is secret) keyed by `(destination,
finding_key)` — note `destination` here is `TicketClient.destination_id()`
(e.g. `"jira:{base_url}:{project_key}"`), so this ledger dedupes correctly
whether two configs got there via embedded credentials or via sharing the
same `ticket_destinations` row; either way they resolve to the same
destination string and correctly collide.

- `claim(destination, finding_key) -> bool` is the atomic compare-and-set:
  `INSERT ... ON CONFLICT (destination, finding_key) DO NOTHING RETURNING
  1`. If a row gets inserted, this caller "won" the claim and should
  proceed to create the ticket; if the conflict fires, it "lost" and
  should skip. Before that insert, it also runs a self-healing `DELETE` on
  any claim that's held a `NULL` `ticket_key` for more than 300 seconds —
  protection against a worker that claimed a finding and then crashed
  before recording a real ticket key.
- `record_ticket(...)` fills in the winning claim's `ticket_key` once
  `create_ticket()` actually succeeds.
- `release(...)` frees an unfinished claim if `create_ticket()` raises, so
  a Temporal retry of the same activity can reclaim it immediately instead
  of waiting out the 300s stale window.

`create_tickets_activity` uses this ledger *before* ever calling Jira's
`find_existing()` label search — the ledger closes the race window;
`find_existing()` remains a secondary fallback for tickets that predate
the ledger or were created out-of-band.

### 7.6 The `cli/stack/` package (`gozu up`/`down [--wipe]`)

This used to be a single ~105-line `cli/stack.py`; it's now a package,
split across six files, adding real reliability fixes and one genuinely
destructive new command:

- **`__init__.py`** — `up()`: `require_initialized()` (exits cleanly with
  a red "run `gozu init` first" message instead of crashing on a
  never-initialized `~/.gozu/stack/` — the cold-restart-crash fix) →
  `ensure_postgres_up(STACK_DIR)` → reads all configs → computes
  `active_profiles(configs)` → `ensure_webhook_secrets(configs)` → `docker
  compose {profile flags} up -d` → prints `docker compose ps` (plus a
  "SonarQube can take 30-60s+" note if that profile is active) →
  `print_webhook_urls(configs)`.
  `down(wipe=False)`: `require_initialized()` → `ensure_postgres_up(...)`
  → computes profiles → **if `wipe`**, gathers preview counts (config
  destinations, dedupe claims, whether SonarQube's volume is in play) via
  `gather_preview()` **before** stopping anything, since counting rows
  needs a live connection and `stop()` stops Postgres too → `stop(profiles,
  STACK_DIR)` (now correctly passes `--profile` flags — previously a bare
  `docker compose stop` couldn't see profile-gated services at all) → if
  not `wipe`, done; otherwise `confirm_and_wipe(...)`.
- **`profiles.py`** — `active_profiles(configs)` (adds `"sonarqube-local"`
  if any config scans locally, `"webhook"` if any uses webhook mode) and
  `profile_flags(profiles)` (sorted `["--profile", name, ...]` pairs — must
  precede the Compose subcommand) are now shared by both `up` and `down`,
  which is exactly what fixed `down`'s missing-profile-flags bug.
  `ensure_service_up(stack_dir, service, profile=None)` runs `docker
  compose [--profile X] up -d --wait {service}` — used both by `gozu up`
  (implicitly, via the full stack) and directly by the wizard's
  SonarQube-startup step (§7.2). `ensure_postgres_up()` is the
  single-service wrapper the wizard and both `up()`/`down()` all share.
- **`webhooks.py`** — the old `_ensure_webhook_secrets`/`_print_webhook_urls`
  logic, extracted unchanged in behavior: generates a
  `secrets.token_urlsafe(32)` secret for any webhook-mode config missing
  one (printed once, "shown once" warning), and prints each webhook
  config's callback URL — `http://receiver:5000/webhooks/sonarqube/{name}`
  (fixed internal Docker port; distinct from the host-side `RECEIVER_PORT`
  mapping), with an extra warning for Cloud+webhook configs that this URL
  is Docker-internal only and must be exposed externally by hand.
- **`wipe.py`** (new — `gozu down --wipe`) — the **one irreversible
  command** in this CLI. `gather_preview(profiles)` counts what would be
  destroyed (`ticket_destinations` count, `ticket_claims` count, whether
  SonarQube's own volume is in scope). `confirm_and_wipe(...)` prints exact
  row counts (not estimates) plus where the pre-wipe backup will land,
  then requires typing the **literal word `wipe`** — anything else cancels
  with zero destructive side effects (the stack still ends up stopped from
  the earlier non-destructive `stop()`, but nothing is deleted). On
  confirmation: restarts Postgres (the plain stop already stopped it),
  `create_backup(...)`, then the actual `docker compose {profile flags}
  down -v` that destroys the volumes.
- **`backup.py`** (new) — `next_backup_path()` builds a timestamped
  `~/.gozu/backups/wipe-<ISO8601>.sql` path once, reused for both the
  pre-confirmation preview text and the actual write, so what's shown
  matches what's written. `create_backup(path, stack_dir)` prunes backups
  older than 7 days (piggybacked on every wipe run, no separate cron), then
  runs `docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER"
  "$POSTGRES_DB"'` (reading credentials from the **container's own**
  environment, not the host shell) with stdout redirected to the backup
  file; on failure, deletes the partial file and raises. Its own docstring
  says this was added **after a real incident**: a fully confirmed,
  deliberately-executed `--wipe` destroyed real dev configs/credentials
  with nothing to restore from.
- **`files.py`** (new — the pip-install packaging piece) — `gozu` needs the
  Docker Compose stack (compose file, Dockerfile, SQL schema, lockfile,
  and the entire Python source tree the worker/receiver images build from)
  to exist somewhere on disk even when installed via `pip install gozu`
  with no git checkout around. `ensure_stack_files()` materializes all of
  that into `~/.gozu/stack/`: a fixed list of static files
  (`docker-compose.yml`, `Dockerfile`, `pyproject.toml`, `uv.lock`,
  `sql/init.sql`) copied from a bundled `cli.stack._stackfiles` package,
  plus every source package (`core`, `scanner`, `ticket`, `temporal`,
  `receiver`, `cli`, `scripts`, `config`) copied fresh from the **running
  installation** via `importlib.resources` (not a second bundled copy),
  since the Docker build needs a real, current source tree to `COPY . .`
  from. Safe to call repeatedly — always re-copies, never touches `.env`
  or any other `~/.gozu/` subdirectory. `is_initialized()` /
  `require_initialized()` check for `~/.gozu/stack/.env` +
  `docker-compose.yml` existing — this is the other half of the
  cold-restart-crash fix (§ above).

  **`cli/stack/_stackfiles/`** in the git checkout is just symlinks back to
  the real root-level files (`Dockerfile`, `docker-compose.yml`,
  `pyproject.toml`, `uv.lock`, `sql/init.sql`) — a single source of truth,
  no hand-kept duplicates.

  > **Note**: `files.py`'s own comment claims this is bundled per
  > `pyproject.toml`'s `[tool.hatch.build.targets.wheel.force-include]` —
  > but no such section actually exists in `pyproject.toml` (only a plain
  > `include = [...]` list of the 8 source packages). That comment is
  > stale/inaccurate. It doesn't matter in practice, though: verified
  > directly by running `uv build` and inspecting the resulting wheel —
  > `hatchling`'s plain `include` glob already dereferences the
  > `_stackfiles/` symlinks into real file bytes (`uv.lock` lands at its
  > full ~185KB, not a broken 0-byte link), and installing that wheel into
  > a clean virtualenv produces a working `gozu` command. So a genuine
  > `pip install gozu` does carry the stack files correctly — only the
  > code comment is wrong, not the packaging itself.

### 7.7 Temporal (`temporal/`)

One `Worker` process (`worker.py`) registers both workflows
(`ScanToTicketWorkflow`, `MultiBranchScanWorkflow`) and all three
activities, and blocks on `worker.run()`. Every model in this codebase —
`Finding`, `TicketResult`, every activity's input model — is a Pydantic
`BaseModel`; `data_converter.py`'s `pydantic_data_converter` (shared by the
worker, the CLI's workflow trigger, and the webhook starter) serializes all
of them across the workflow/activity boundary automatically. Workflow code
(`workflows/`) uses `workflow.logger`; everything that runs inside an
activity (`activities/` and the `scanner`/`ticket` modules they call into)
uses `activity.logger` — a fixed project convention (`CLAUDE.md`); the
only exceptions are the two process entrypoints (`receiver/app.py`,
`temporal/worker.py`) which use plain `logging` since they run before any
Temporal context exists.

### 7.8 Receiver (`receiver/`)

Covered fully in §4. Worth calling out: there's **no CSRF protection**,
deliberately — the app has no cookies, sessions, or HTML forms for a
forged cross-site request to exploit; every route is authenticated purely
by a per-config HMAC signature an attacker can't produce without that
config's `webhook_secret`.

### 7.9 Scripts (`scripts/`)

**`scripts/paths.py`** (new) is the one host-side state directory every
part of gozu shares: `GOZU_HOME = Path.home() / ".gozu"` and `STACK_DIR =
GOZU_HOME / "stack"` — used by the Java/sonar-scanner prerequisite
downloaders, `cli/stack/files.py`, `scripts/env_ports.py`, and
`cli/stack/backup.py`. `STACK_DIR` is deliberately a subdirectory of
`GOZU_HOME`, not flat, to avoid colliding with an unrelated `temporal` CLI
binary a user might separately keep under `~/.gozu/` — this repo's own
`temporal/` source package gets copied into `STACK_DIR` by
`ensure_stack_files()`.

`bootstrap_env.py` provisions `.env` field-by-field, never overwriting an
existing value; `.env`'s path (`scripts/env_ports.py`'s `ENV_PATH`) is now
`STACK_DIR / ".env"` rather than a repo-relative path, since a pip install
has no repo checkout for a relative path to land in, and
`docker-compose.yml`'s `env_file: .env` needs it alongside itself.
`fernet_safety.py`'s safety check is unchanged (see §7.3).
`seed_test_config.py` is still a throwaway round-trip check (create a
config, read it back, verify every field/credential round-trips through
encryption, always delete itself) — **it was not extended to exercise the
new `ticket_destinations` path**; it only covers the legacy
embedded-credentials shape.

---

## 8. GitHub Actions — what's actually there

There are exactly two workflow files, and neither one "builds the tool" in
a generative sense — they're standard CI and release automation, not code
generation:

### `.github/workflows/ci.yml` — runs on every push to `main` and every PR

```yaml
on:
  push: { branches: [main] }
  pull_request:
```

Steps: checkout → install `uv` (via `astral-sh/setup-uv`) → install Python
3.12 → `uv sync` (installs every dependency pinned in `uv.lock`) → `uv run
pytest -v`. That's the entire pipeline: it exists purely to make sure the
existing test files (§12) still pass before anything merges to `main`.
There's no linting, type-checking, or build step in CI itself (those exist
as manual `uv run ruff check .` / `uv run --with pyright pyright .`
commands documented in `README.md`, but nothing runs them automatically).

### `.github/workflows/release.yml` — runs only when a `v*` tag is pushed

```yaml
on:
  push: { tags: ["v*"] }
```

Triggered by `git tag v0.2.3 && git push origin v0.2.3`. Steps:

1. Checkout, install `uv` + Python 3.12 (same as CI).
2. **Version guard**: extracts the tag name minus its `v` prefix and
   compares it against `pyproject.toml`'s `[project].version` (read via
   `tomllib`); fails the whole run with an `::error::` annotation if they
   don't match exactly. This exists so a release can never accidentally
   ship under the wrong version string — the tag and the package metadata
   are forced to agree.
3. `uv sync` and `uv run pytest -v` again — a release build re-runs the
   full test suite rather than trusting a prior CI green check.
4. `uv build` — produces a wheel and sdist into `dist/` via the
   `hatchling` build backend configured in `pyproject.toml`. Verified
   directly (§7.6) that the resulting wheel does carry a working
   `cli/stack/_stackfiles/` and installs into a clean environment as a
   functioning `gozu` command.
5. `gh release create "$GITHUB_REF_NAME" dist/* --title "$GITHUB_REF_NAME"
   --generate-notes` — creates a **GitHub Release** (not a PyPI publish)
   with the built wheel/sdist attached as downloadable assets, and
   auto-generated release notes from the commit history since the last
   tag.

**In short**: GitHub Actions here provides (a) a merge gate that runs
`pytest` on every PR/push, and (b) a one-command release process — tag a
commit, and a GitHub Release with built artifacts appears automatically,
guaranteed to match the version declared in the source. It does not build
Docker images, does not deploy anything, and does not publish to PyPI —
distribution is purely through GitHub Releases' attached wheel/sdist.

Nothing else in the codebase (`cli/`, `scripts/`, anywhere) references
GitHub Actions, reads a `GITHUB_*` environment variable, or contains any
separate build/release tooling — these two files are the entire CI/CD
story for this project.

---

## 9. The copyright-header convention

Most of the file-level churn from a recent large pull (99 files changed,
but nearly all by only 3-6 lines) comes from a new, explicit `CLAUDE.md`
convention: every source file (except Markdown docs and vendored/generated
files like `uv.lock`) now carries a header, in the file's native comment
syntax, placed after a shebang if present or otherwise as the very first
thing in the file (before even a module docstring):

```python
# Copyright (c) 2026 Calfus Inc.
# Author: <original author's real name>
# Editor: <anyone else who has since modified this file, comma-separated>
```

No license line — company ownership is deliberately left unresolved for
now. `Author` is the original writer (a real human name, from git
identity — never an AI tool); `Editor` lists anyone who's since modified
the file and is omitted entirely if there isn't one yet. There's also an
optional **"Depends on" line** under a blank `#`, added only when a file
makes direct API/SDK calls to one of four vendor dependencies —
SonarQube, Jira, Temporal, or PostgreSQL — and only for the file actually
making that call, not merely adjacent code. You can see it applied, e.g.,
in `config/connection.py`, `config/ticket_destinations.py`,
`config/store.py`, and `cli/stack/backup.py`, all tagged `# Depends on:
PostgreSQL - data storage`.

---

## 10. Docker Compose / infrastructure

`docker-compose.yml` defines five services and now pins **`name: gozu`**
explicitly at the top level — without it, Compose derives the project
name from the compose file's parent directory, which for a pip install is
`~/.gozu/stack/` rather than a repo checkout named after the repo, which
would otherwise produce confusingly-named containers/volumes/networks.

- **`postgres`** — always on. Stores every saved config (encrypted
  credentials), the ticket-destinations table, and the ticket-claims
  ledger. Schema loads once, on first volume initialization, from
  `sql/init.sql`.
- **`temporal`** — always on. Runs Temporal's official all-in-one dev
  server (in-memory/SQLite persistence) — explicitly a local/dev setup,
  not for production use.
- **`sonarqube`** — only under the `sonarqube-local` profile. The
  official `sonarqube:community` image. Its healthcheck now uses `curl -sf
  http://localhost:9000/api/system/status | grep -q '"status":"UP"'`
  instead of a `wget`-based check — `wget` was removed from the current
  base image, so the old healthcheck failed every single time regardless
  of whether SonarQube itself was actually healthy. This is a real,
  previously-broken healthcheck fixed in this pull.
- **`worker`** — always on; builds from the repo's own `Dockerfile`, runs
  `python -m temporal.worker`. Has `host.docker.internal` wired in via
  `extra_hosts` so it can reach a host-side "local" SonarQube even on
  Linux.
- **`receiver`** — only under the `webhook` profile; reuses the exact same
  image as `worker`, just runs `python -m receiver.app` instead.

The `Dockerfile` itself is worth noting for one deliberate ordering
decision: `playwright install --with-deps chromium` runs *before* `COPY .
.`, so a source-only code change doesn't force a ~90-second Chromium
re-download on every rebuild — it only invalidates when the pinned
Playwright version in `uv.lock` changes.

---

## 11. Environment variables

Two very different categories exist today, and conflating them is the
single biggest way `.env.example` misleads (it predates the config-store
architecture entirely):

**Still genuinely required, read directly from the process environment:**

| Variable | Used by |
|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` / `POSTGRES_HOST` / `POSTGRES_PORT` | `config/connection.py` (shared by `config/store.py`, `config/ticket_destinations.py`) and `ticket/claims.py`'s own duplicate connection helper |
| `FERNET_KEY` | `config/crypto.py` — encrypts/decrypts every stored credential |
| `TEMPORAL_HOST` | `temporal/worker.py`, `receiver/starter.py` (Compose sets this to `temporal:7233` inside the network; the CLI's own `workflow_trigger.py` instead uses `localhost:{TEMPORAL_PORT}` since it runs outside Docker) |
| `SONARQUBE_PORT` / `TEMPORAL_PORT` / `TEMPORAL_UI_PORT` / `RECEIVER_PORT` | port mappings, written by `scripts/bootstrap_env.py`; not documented in `.env.example` at all |

**Legacy fallback-only, effectively vestigial in normal usage today:**
`SONAR_WEBHOOK_SECRET`, `SCANNER_TYPE`, `SONAR_HOST_URL`, `SONAR_TOKEN`,
`SONAR_ORGANIZATION`, `TICKET_BACKEND`, `JIRA_URL`, `JIRA_EMAIL`,
`JIRA_API_TOKEN`, `JIRA_PROJECT_KEY`. These are only consulted by
`scanner/factory.py`'s `get_scanner_client()` and `ticket/factory.py`'s
`get_ticket_client()`, which exist purely as a fallback for the case where
a workflow input arrives with an **empty** `credentials` dict. Both the
CLI and the webhook receiver always pass through a specific config's real,
decrypted credentials in normal operation, so this fallback path
doesn't actually fire any more — kept for backward compatibility only.
(One narrow exception: the screenshot activity still falls back to reading
`SONAR_TOKEN` from the environment if `credentials` doesn't include a
`sonar_token`.)

**Actual per-config credential keys** (stored encrypted, set via `gozu
init`, never in `.env`): `sonar_host_url`, `sonar_token`,
`sonar_organization`, `webhook_secret` — plus, for a **destination's**
credentials specifically (`ticket_destination_credentials`, resolved
through `config/store.py`'s `get_config()`): `jira_url`, `jira_email`,
`jira_api_token` (and `jira_project_key` lives on the `ticket_destinations`
row itself, not as a credential).

---

## 12. Testing — what's covered and what isn't

The entire automated test suite is two files:

- **`tests/scanner/test_screenshot.py`** — mocks Playwright's
  `Page`/`Locator` objects to verify: successful code-snippet extraction;
  graceful `None` fallback when extraction raises; correct
  "last `<header>` wins" annotation-text disambiguation (and `None` when no
  headers exist, or when the lookup itself raises).
- **`tests/temporal/activities/test_capture_and_attach_screenshot.py`** —
  verifies `add_comment`/`attach_screenshot` are each skipped cleanly
  (logged warning, no exception) when a fake ticket client doesn't support
  them, that a *real* error from a method that does exist still propagates
  (so Temporal's retry policy actually gets a chance to see it), and that
  the activity's temp directory is removed via its `finally` block even
  when an exception propagates.

`pytest.ini` sets `asyncio_mode = auto`, so async test functions need no
`@pytest.mark.asyncio` decorator.

**Everything else has zero automated coverage**, and the new
shared-ticket-destinations feature widens this gap further:
`scripts/seed_test_config.py` (the only thing that even manually exercises
`config/store.py`/`config/crypto.py` against a live Postgres) was **not
updated** to create or round-trip a `ticket_destinations` row — it still
only covers the legacy embedded-credentials shape. Also still untested:
the entire `cli/` package (wizard, scan runner, prerequisites, and now the
whole `cli/stack/` package including the new `--wipe` path), `ticket/claims.py`'s
actual concurrent-claim behavior, `receiver/app.py`'s branch-glob gating,
both scanner backend implementations, the Jira client/sprint-assignment
logic, and both Temporal workflows.

---

## 13. Known limitations (still true today)

- **SonarQube Cloud's Free plan has no push mechanism** — `watch` mode is
  a straightforward polling workaround, not a real webhook integration.
- **Self-hosted Community Build has no real multi-branch analysis**, and
  neither does Cloud Free — branch tagging only happens for Premium
  configs; `MultiBranchScanWorkflow` fanning out against non-Premium
  underlying data means every child queries identical findings, and only
  the claims ledger prevents duplicate tickets.
- **No ticket lifecycle beyond creation** — if a SonarQube issue is later
  resolved, nothing updates or closes the corresponding Jira ticket.
- **Screenshot selectors are SonarQube-version-specific** — discovered by
  live trial-and-error against one SonarQube build; a future frontend
  change may silently break them (degrades to a full-page screenshot with
  no text extraction rather than failing loudly).
- **`FERNET_KEY` loss is unrecoverable** — by design, no backdoor exists.
- **`cli/stack/files.py`'s comment about `force-include` is stale** — no
  such `pyproject.toml` section exists, but this is harmless: verified
  directly that `uv build` + a clean-venv install still produces a fully
  working `gozu` command (§7.6), so the comment is just out of date, not a
  real packaging gap.
- **The connection-helper centralization is partial** — `ticket/claims.py`
  still duplicates its own Postgres connection logic instead of using the
  new shared `config/connection.py`.
- **`scripts/seed_test_config.py` doesn't cover ticket destinations** —
  the shared-board feature has no automated or even manual round-trip
  check.
- **The "bonus method" `getattr` dispatch pattern has nothing to actually
  exercise it** — `JiraClient` is the only `TicketClient` implementation
  that exists today.
- **`gozu down --wipe` is genuinely irreversible for anything outside the
  pre-wipe Postgres backup** — SonarQube's own volume (issues, project
  history) is destroyed with no backup taken of it at all, only Postgres
  gets `pg_dump`'d.
