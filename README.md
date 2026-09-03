# sonar-to-jira (local, zero-cost POC)

Proof of concept for a pipeline that will eventually auto-create Jira
tickets from SonarQube vulnerabilities/hotspots, orchestrated with
Temporal. Everything below runs locally and free.

**Current scope:** webhook fires -> signature verified -> Temporal workflow
starts -> an activity fetches real vulnerabilities/hotspots from local
SonarQube -> a second activity creates a Jira ticket for each issue that
doesn't already have one (dedupe via a label), skipping the rest, and
drops new tickets straight into the project's active sprint if one exists
-> a third activity screenshots each newly-created ticket's flagged code
in the SonarQube UI, attaches it to the ticket, and (best-effort, in the
same Playwright session) extracts the flagged code and SonarQube's inline
issue annotation as text, posting them as a ticket comment. The screenshot
activity runs concurrently per ticket, with its own (more lenient) timeout
and retry policy, and its failures are isolated per-ticket - a broken
screenshot never fails the workflow run or affects ticket creation/dedupe,
and a failed text extraction degrades to no comment rather than failing
the screenshot/attach step. Findings are also stamped with the git branch
checked out in the worker's own working directory (SonarQube Community
Build doesn't report branch info via its API) and it's surfaced in the
ticket description.

## Architecture

```
SonarQube (Docker)
   |  webhook (HMAC-SHA256 signed)
   v
receiver/app.py (Flask, :5001)
   |  verifies signature (receiver/verify_signature.py),
   |  starts workflow via receiver/starter.py
   v
Temporal server (:7233)
   |
   v
temporal/worker.py -> temporal/workflows/scan_to_ticket.py (ScanToTicketWorkflow)
                  |
                  v
             temporal/activities/fetch_findings.py
                  |
                  v
             scanner/client.py (ScannerClient interface)
                  |
        -------------------
        |                 |
 SonarQubeServerClient    SonarQubeCloudClient (stub, NotImplementedError)
   (self-hosted, real)
                  |
                  v
             temporal/activities/create_tickets.py
                  |
                  v
             ticket/client.py (TicketClient interface)
                  |
                  v
             JiraClient
                  |
                  v
             Jira Cloud (find_existing dedupe, then create_ticket,
                         then add to the project's active sprint if one exists)
                  |
                  v  (for each newly-created ticket, concurrently)
             temporal/activities/capture_and_attach_screenshot.py
                  |
                  v
             scanner/screenshot.py (Playwright, async API) -> screenshot of
             the finding's source viewer panel, plus best-effort extraction
             of the flagged code + inline issue annotation as text
                  |
                  v
             ticket/client.py (JiraClient.attach_screenshot) -> attaches
             sonarqube-{finding-key}.png to the ticket, then
             (JiraClient.add_comment) -> posts the extracted text as a
             comment, if any was extracted
```

The pipeline is generic over which scanner and which ticketing system are
behind it - SonarQube and Jira are just the only implementations that
exist today.

- **`core/models.py`** defines the normalized vocabulary every adapter speaks:
  `Severity` (CRITICAL/HIGH/MEDIUM/LOW/INFO) and `Finding`. Neither side of
  the pipeline ever sees a tool-specific value (SonarQube's
  BLOCKER/MAJOR/etc, or any future tool's own scale) outside its own
  adapter.
- **`scanner/client.py`** is the scanner extension point: `ScannerClient`
  (ABC, one method - `fetch_findings(project_key) -> list[Finding]`),
  implemented today by `SonarQubeServerClient` (real) and
  `SonarQubeCloudClient` (stub, raises `NotImplementedError`), selected by
  `get_scanner_client()` based on `SCANNER_TYPE`. Adding a new scanner
  means implementing `ScannerClient` and registering it in
  `get_scanner_client()` - no other file changes.
- **`ticket/client.py`** is the ticketing extension point: `TicketClient`
  (ABC, `find_existing(finding_key)` + `create_ticket(finding)`),
  implemented today by `JiraClient`, selected by `get_ticket_client()`
  based on `TICKET_BACKEND`. Adding a new ticket destination means
  implementing `TicketClient` and registering it in `get_ticket_client()`
  - no other file changes.

Nothing in the `temporal/` package or `receiver/` needs to change to add a
new scanner or ticket backend - they only ever talk to `ScannerClient` /
`TicketClient` / `Finding`.

Screenshot capture/attach and text extraction/comment (`scanner/screenshot.py`,
`ticket/client.py`'s `attach_screenshot` and `add_comment`) are bonus
capabilities layered on top, not part of either abstract interface - they're
called directly from `capture_and_attach_screenshot_activity` rather than
through `get_scanner_client()`/`get_ticket_client()`, since they're specific
to SonarQube's UI and Jira's attachment/comment APIs and aren't guaranteed
to exist on every future scanner/ticket backend. `capture_finding_screenshot`
returns a `FindingExtraction` (screenshot path + best-effort code
snippet/annotation text); `attach_screenshot` and `add_comment` are each
dispatched independently via `getattr()`, so a future `TicketClient` missing
either one just skips that step instead of crashing.

Switching from self-hosted SonarQube to SonarQube Cloud later should mean:

1. Implementing `fetch_findings()` on `scanner/client.py`'s `SonarQubeCloudClient`.
2. Setting `SCANNER_TYPE=sonarqube-cloud` in `.env`.

**Serialization note:** every type that crosses a Temporal workflow/activity
boundary (`Finding`, `CreatedTicket`, `TicketResult`, `SonarToJiraInput`,
`ScreenshotAttachInput`) is a Pydantic `BaseModel`, handled automatically by
the Pydantic-aware data converter in `temporal/data_converter.py` - no
manual dict conversion needed anywhere in the pipeline. `Severity` is an
`Enum` field on `Finding`, which Pydantic serializes natively.

**Logging note:** this project uses exactly one logging mechanism inside the
Temporal-executed pipeline - `workflow.logger` in `scan_to_ticket.py`,
`activity.logger` everywhere else that runs inside an activity (`scanner/`,
`ticket/`, `temporal/activities/`). Both are Temporal's contextual loggers,
so every log line is automatically tagged with workflow/activity id, run id,
etc. Plain `logging.basicConfig`/`logging.getLogger` is used only in the two
process entrypoints (`receiver/app.py`, `temporal/worker.py`), since there's
no Temporal activity/workflow context to attach to before a workflow starts
or before the worker begins running.

## Phase 1: Infrastructure

This project is being rebuilt into a commercial CLI tool, working name
`codescan` (placeholder; the repo/working directory stays `sonar-to-jira`
until a real name is picked). This is Phase 1 of that build:
**infrastructure only** - a Docker Compose stack that boots cleanly with
an empty, seeded Postgres database, plus a verified encrypted credential
store. There is no CLI wizard yet (that's Phase 2, below) - the sections
further down (steps 1-10) describe the older, fully-manual local workflow
and still work independently of this stack.

**What got added:**

- **`crypto_utils.py`** - `encrypt_token()`/`decrypt_token()` using Fernet
  (from the `cryptography` library), keyed by a `FERNET_KEY` environment
  variable. Raises a clear error (not a silent no-op) if `FERNET_KEY` is
  missing or malformed.
- **`sql/init.sql`** - two tables, deliberately normalized: `configs`
  (one named set of scanner + ticket-backend credentials - `id`, `name`,
  `scanner_type`, `scanner_mode`, `ticket_backend`, `trigger_mode`,
  timestamps) and `config_credentials` (arbitrary encrypted key/value
  pairs per config - `sonar_token`, `jira_api_token`, `webhook_secret`,
  whatever a given backend needs). Backend-specific fields live as rows
  in `config_credentials`, not dedicated columns on `configs`, so adding
  a new scanner or ticket backend never needs a schema change. Called
  "configs", not "profiles" - Docker Compose already has an unrelated
  concept called profiles (for conditionally starting services; see the
  `sonarqube` service below), and reusing that word here would be
  confusing throughout the codebase and docs. Every `config_credentials`
  value is stored as Fernet ciphertext, never plaintext. Mounted into the
  postgres container's `/docker-entrypoint-initdb.d/`, so it runs
  automatically the first time the `postgres_data` volume is initialized.
- **`config_store.py`** - psycopg3-based CRUD: `create_config()` inserts
  the `configs` row plus one `config_credentials` row per credential
  (each encrypted), all in a single transaction; `get_config()` returns
  the `configs` columns plus a nested, decrypted `credentials` dict, or
  `None` for a miss (never raises); `list_configs()` returns only
  non-secret metadata (name/scanner_type/scanner_mode/trigger_mode, for a
  picker list, not for use); `delete_config()` cascades to
  `config_credentials` via the FK; `count_configs()` is a plain row
  count, used by `bootstrap_env.py`'s `FERNET_KEY` safety check below.
  All parameterized queries, no string-built SQL.
- **`scanner/client.py`** - `ScannerClient` gained an abstract
  `requirements() -> ScannerRequirements` method (`docker_services`,
  `host_dependencies`), plus a `SCANNER_REGISTRY` dict (scanner_type ->
  display name) that's the single source of truth for "which scanners
  exist" - the init wizard's scanner-selection prompt reads this instead
  of hardcoding "sonarqube".
- **`Dockerfile`** - containerizes `temporal/worker.py`, built via `uv`
  (see Phase 2's packaging section). Sets `PYTHONUNBUFFERED=1` (otherwise
  the worker's log lines never flush inside a container) and
  `PYTHONPATH=/app` (so activity/scanner/ticket imports and scripts under
  `scripts/`/`cli/` resolve without extra flags).
- **`docker-compose.yml`**:
  - `postgres` (`postgres:16-alpine`) and `temporal`
    (`temporalio/admin-tools`, running `temporal server start-dev` - the
    same one-box dev server as the CLI, just containerized) are always-on,
    no profile tag.
  - `sonarqube` (`sonarqube:community`) is tagged `profiles:
    ["sonarqube-local"]`, so it only starts when that profile is
    explicitly requested - most scanner backends (e.g. SonarQube Cloud)
    won't need a local container at all.
  - `worker` is built from the `Dockerfile`, always-on, and waits on both
    `temporal` and `postgres` being `service_healthy` before starting.
  - All four services read from the same `.env`. Host ports for
    postgres/sonarqube/temporal/temporal's web UI are read from
    `${POSTGRES_PORT:-5432}`/`${SONARQUBE_PORT:-9000}`/
    `${TEMPORAL_PORT:-7233}`/`${TEMPORAL_UI_PORT:-8233}` - see
    `bootstrap_env.py` below for how those get chosen. The container-internal
    ports never change; only the host-side published port does.
- **`scripts/bootstrap_env.py`** - generates/merges `.env`, field by
  field, not all-or-nothing: `POSTGRES_USER`/`PASSWORD`/`DB`/`HOST`, the
  four ports above, and `FERNET_KEY` are each filled in independently
  only if missing, so re-running it is always safe and never clobbers a
  value you (or an earlier run) already set. Ports are auto-probed with a
  plain TCP connect and incremented past whatever's already taken on the
  host. `FERNET_KEY` gets an extra safety check before being generated:
  if it's missing and Postgres is reachable *and* already has encrypted
  config rows, generation is refused (would make those rows permanently
  unreadable) unless you pass `--force-new-key` and type `yes` at an
  explicit confirmation prompt. The core logic (`resolve_ports()`,
  `bootstrap_env()`) is importable, not just a script - Phase 2's
  `codescan init` wizard calls it directly so it can show you the chosen
  ports and let you override them before anything is written.
- **`scripts/seed_test_config.py`** - throwaway verification script (not
  part of the product): inserts one dummy config (config fields +
  credentials), reads it back, and confirms every field round-tripped
  correctly - proving the encrypt-on-write/decrypt-on-read path works end
  to end against a real Postgres instance.

### Running it

1. **Generate `.env`** (once per checkout, safe to re-run any time):

   ```bash
   uv sync   # see Phase 2 below for installing uv
   uv run python scripts/bootstrap_env.py
   ```

   It'll print whichever ports/fields it actually wrote. If you already
   have a `.env` (e.g. with `SONAR_TOKEN`/`JIRA_*` from the older manual
   workflow further down this README), nothing in it is touched - only
   whatever's still missing gets added.

2. **Bring up the stack:**

   ```bash
   docker compose up -d
   ```

   Add `--profile sonarqube-local` if you also want a local SonarQube
   container:

   ```bash
   docker compose --profile sonarqube-local up -d
   ```

3. **Confirm health:**

   ```bash
   docker compose ps
   ```

   `postgres` and `temporal` should show `healthy`; `worker` should show
   `Up` (it has no separate healthcheck - `docker compose logs worker`
   should show `Worker started, listening on task queue 'sonar-jira-queue'...`
   with no crash/restart loop). If you started it, `sonarqube` takes
   30-60s+ to go `healthy` (it polls `/api/system/status` for `"UP"`).

   Confirm both tables exist and are empty:

   ```bash
   docker compose exec postgres sh -c '
     psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
       -c "SELECT count(*) FROM configs;" -c "SELECT count(*) FROM config_credentials;"
   '
   # count
   # -------
   #     0
   ```

4. **Prove the encryption round-trip:**

   ```bash
   docker compose exec worker python scripts/seed_test_config.py
   ```

   Successful output looks like:

   ```
   Creating config 'seed-test-XXXXXXXX'...
   Created config id=1
   Reading it back and decrypting...
   Cleaned up config 'seed-test-XXXXXXXX'
   SUCCESS: all 11 fields round-tripped correctly (credentials encrypted on write, decrypted on read, all values match).
   ```

   The script cleans up its own row, so both tables are empty again
   afterward and it's safe to re-run.

**`.env` must never be committed** - `.gitignore` already includes it (it
holds the generated Postgres password, the Fernet key, and whatever real
SonarQube/Jira credentials you fill in).

## Phase 2: Setup Wizard

Phase 2 adds `codescan init`: a fully interactive wizard (`cli/` package,
built with [typer](https://typer.tiangolo.com/) for the command and
[questionary](https://questionary.readthedocs.io/) for every select/confirm
prompt - arrow-key menus, not typed option strings) that provisions
`.env`, checks/installs prerequisites, walks through scanner and
credential selection, and seeds one row via `config_store.create_config()`.
It ends at "a named config exists in Postgres, ready to be used later" -
`codescan run` (actually running a scan) is a later phase and is stubbed
to say so.

**What got added:**

- **`cli/help_links.py`** - a `HELP_LINKS` dict mapping a wizard field
  name to a real, current doc URL for obtaining that value (SonarQube
  token generation, SonarQube Cloud organization key, Jira API token,
  etc - every URL was looked up live, not guessed), plus
  `print_help_link()`, a small helper that prints a dimmed `-> docs:
  <url>` line before the prompt that needs it.
- **`cli/prerequisites.py`** - `check_java()` (attempts `java -version`
  on PATH) and `ensure_java()`: if no usable Java is found (checking a
  previously-downloaded managed JRE first, so this doesn't redownload on
  every run), explains that sonar-scanner needs a JVM and downloads a
  portable Eclipse Temurin JRE build (correct for the host's OS/arch, via
  Adoptium's API) into `~/.codescan/jre/` - no system-wide install,
  fully contained to that directory - then confirms it's usable
  afterward.
- **`cli/init_wizard.py`** - the interactive flow: bootstrap `.env`
  (showing the auto-detected ports and letting you override each one
  before anything's written), `ensure_java()`, choose a scanner (sourced
  from `scanner/client.py`'s `SCANNER_REGISTRY`, currently just
  SonarQube), Local or Cloud, then:
  - **Local** prompts for a host URL (default `http://localhost:9000`)
    and a token, and - since local self-hosted SonarQube is exactly what
    the existing webhook receiver (`receiver/app.py`) is built for -
    collects a webhook secret and sets `trigger_mode="webhook"`.
  - **Cloud** asks Free or Premium, then always collects an organization
    key + token. **Premium** offers a webhook (secret + which branch to
    scope, noting branch analysis is a Premium-only SonarQube Cloud
    feature) or falls back to `trigger_mode="direct"`. **Free** can't use
    webhooks at all (SonarQube Cloud's Free plan doesn't invoke them), so
    instead picks a trigger mode from "Direct (your own CI)", "GitHub
    Actions signal + poll", or "Scheduled watch polling".

  Then Jira details (URL, email, API token, project key, each with a
  help link), a config name (checked against `list_configs()` - a
  duplicate name is rejected and re-prompted, never silently
  overwritten), and finally `config_store.create_config(...)` with
  whatever credentials were actually collected. The closing summary
  prints names/modes only, never secret values.
- **`cli/main.py`** - the `codescan` typer app, registering `init`. `run`
  and `down` are registered too, each just printing "not yet implemented
  - coming in a later phase", so `codescan --help` already shows the
  tool's intended shape.

**Packaging moved from pip/`requirements.txt` to
[uv](https://docs.astral.sh/uv/):**

- `pyproject.toml` declares every dependency under `[project.dependencies]`
  and registers the `codescan` console script (`cli.main:app`).
  `uv.lock` is the pinned-version source of truth (`requirements.txt` is
  gone - nothing else in the repo reads it).
- The worker `Dockerfile` now builds via `uv`: it copies the `uv`/`uvx`
  binaries from `ghcr.io/astral-sh/uv`'s official image, then runs `uv
  sync --frozen` against `pyproject.toml`/`uv.lock` (with
  `UV_PROJECT_ENVIRONMENT=/usr/local`, so the installed packages and the
  `codescan` command land directly in the image's system Python - no
  separate venv to activate inside the container).

### Installing locally

```bash
# install uv if you don't have it: https://docs.astral.sh/uv/getting-started/installation/
uv sync
```

This creates/updates `.venv` with every dependency from `pyproject.toml`/
`uv.lock` and installs the `codescan` command into it. Run it with `uv run
codescan ...`, or activate the venv (`source .venv/bin/activate`) and run
`codescan ...` directly.

### Running the wizard

```bash
uv run codescan init
```

**A full Local walkthrough** looks like: accept (or override) the
auto-detected ports -> `.env` is provisioned/merged -> Java is checked
(and a portable JRE downloaded into `~/.codescan/jre/` if none is found)
-> only one scanner is registered, so SonarQube is picked automatically
-> "Local" -> host URL (default `http://localhost:9000`) + token -> a
webhook secret (leave blank to have one generated and shown once) ->
Jira URL/email/API token/project key -> a config name -> summary. The
resulting config has `scanner_mode="local"`, `trigger_mode="webhook"`.

**A full Cloud Free-plan walkthrough** differs after the Local/Cloud
choice: "Cloud" -> "Free" -> organization key + token -> a note that
webhooks aren't offered on the Free plan -> pick a trigger mode (Direct/
GitHub Actions poll/Scheduled watch) -> Jira details -> config name ->
summary. The resulting config has `scanner_mode="cloud"`, `trigger_mode`
matching whichever of `direct`/`github_poll`/`watch` was chosen, and its
credentials have `sonar_organization` but no `webhook_secret`.

### Confirming success

Query the tables directly (same spirit as Phase 1's
`scripts/seed_test_config.py`, now exercised through the real wizard):

```bash
docker compose exec postgres sh -c '
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -c "SELECT name, scanner_type, scanner_mode, trigger_mode FROM configs;" \
    -c "SELECT config_id, key, left(value, 12) || '"'"'...'"'"' AS ciphertext_preview FROM config_credentials;"
'
```

The `configs` row should match what you chose in the wizard; every
`config_credentials.value` should be Fernet ciphertext (starts with
`gAAAAAB...`), never a plaintext secret.

Then confirm `get_config()` round-trips the decrypted values correctly,
the same way `seed_test_config.py` does it:

```bash
docker compose exec worker python -c "
import config_store
result = config_store.get_config('<the name you gave it in the wizard>')
print(result['scanner_type'], result['scanner_mode'], result['trigger_mode'])
print(sorted(result['credentials'].keys()))
"
```

If a credential you entered (e.g. the Jira project key) comes back
exactly as typed, the wizard's encrypt-on-write/decrypt-on-read path
works end to end.

**Testing the `FERNET_KEY` safety check:** with at least one config
already created, remove the `FERNET_KEY=...` line from `.env` and
re-run bootstrap:

```bash
uv run python scripts/bootstrap_env.py
```

This should refuse (non-zero exit) and explain that Postgres already has
encrypted config row(s) and no key was found - `.env` is left untouched.
Re-running with `uv run python scripts/bootstrap_env.py --force-new-key`
prompts for an explicit `yes` before generating a replacement key (which
makes the earlier config's credentials permanently undecryptable - that's
the point of the check).

## Phase 3: Running Scans

Phase 3 adds `codescan run`: scan, wait for SonarQube's *server-side*
processing to actually finish (not just the scanner subprocess exiting),
then trigger `ScanToTicketWorkflow` directly - no webhook involved. Plus
`--watch` for looping it on an interval.

**Two retroactive fixes landed first** (found while designing this phase):

- **`project_key`** is now part of every config - `configs.project_key`
  (nullable at the DB level via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`,
  so `test1`/`test1cloud` from Phase 2 aren't broken by a `NOT NULL`
  constraint), but a required wizard prompt for any *new* config. Neither
  wizard path used to collect a project key at all - the old webhook
  payload carried the project key implicitly; direct invocation has no
  such payload, so `codescan run` needs it explicitly for both the
  `sonar-scanner` command and the issues-fetch API call.

  **`test1` and `test1cloud` predate this field and have no project_key -
  recreate them with `codescan init` before using them with `codescan run`.**
- The wizard's Local-mode host URL default now reads the actual
  `SONARQUBE_PORT` `bootstrap_env.py` resolved (auto-incremented past a
  port conflict) instead of a hardcoded `http://localhost:9000`, which
  was wrong whenever 9000 was taken.

**Two real bugs were found and fixed via live testing** while building this
(not just found in code review - both only showed up running a real scan
against a real config):

1. **`fetch_findings_activity`/`create_tickets_activity` didn't know about
   per-config credentials at all.** They called `scanner/client.py`'s
   `get_scanner_client()`/`ticket/client.py`'s `get_ticket_client()`, which
   read `SONAR_HOST_URL`/`SONAR_TOKEN`/`JIRA_*` from the *worker
   container's own* `.env` - not from whichever config `codescan run`
   selected. Fixed by extending `SonarToJiraInput` (and two new narrow
   per-activity models, `FetchFindingsInput`/`CreateTicketsInput`) to carry
   the selected config's `scanner_type`/`scanner_mode`/`ticket_backend`/
   `credentials`, and splitting each client factory into a pure
   `build_scanner_client()`/`build_ticket_client()` (explicit params, no
   env reads) plus a thin `get_scanner_client()`/`get_ticket_client()` env
   wrapper around it for the webhook receiver, which still has no concept
   of a "config" and is unaffected (empty `credentials` falls back to the
   env-var path exactly as before).
2. **`localhost` in a config's `sonar_host_url` is unreachable from inside
   the worker container.** `codescan run` (and SonarQube itself) run on
   the host; the worker runs in Docker Compose's own network. Fixed with
   `scanner/client.py`'s `resolve_container_host()`, applied only to
   requests the worker process makes itself (SonarQube API calls,
   `scanner/screenshot.py`'s Playwright navigation) via
   `host.docker.internal` - **never** to a `Finding.deep_link`, which stays
   human-facing (shown in Jira ticket descriptions, must resolve from a
   real browser outside Docker). `docker-compose.yml`'s `worker` service
   got an `extra_hosts: host.docker.internal:host-gateway` entry so this
   resolves on Linux too (native on Docker Desktop already).

**What got added:**

- **`cli/prerequisites.py`**: `check_sonar_scanner()`/`ensure_sonar_scanner()`,
  same shape as `check_java()`/`ensure_java()` - downloads the official
  sonar-scanner CLI distribution for the host's OS/arch into
  `~/.codescan/sonar-scanner/` if none is found on PATH or from a previous
  download, no system-wide install. `java_env()` builds a `JAVA_HOME`/`PATH`
  environment for running sonar-scanner, so a managed JRE actually gets
  found by its launcher script.
- **`cli/scan_runner.py`**: `select_config(name)` (explicit name, auto-select
  the only one, or an informed questionary select among several) and
  `run_scan_cycle(config, path)` - the full scan-to-ticket cycle: ensure
  prerequisites, build and run the `sonar-scanner` command from the
  config (Local: `sonar_host_url` + token; Cloud: `sonarcloud.io` +
  organization + token), read `.scannerwork/report-task.txt`'s `ceTaskId`,
  poll `GET {host}/api/ce/task?id={ceTaskId}` every 2s (5 minute timeout,
  `FAILED`/`CANCELED` raise clearly) until `SUCCESS`, then trigger
  `ScanToTicketWorkflow` with workflow ID `sonar-jira-{ceTaskId}` (same
  deterministic-ID idempotency pattern the webhook receiver uses) and wait
  for its result.
- **`cli/main.py`**'s `run` command: `--config`/`-c` (optional - auto/interactive
  select if omitted), `--watch` (loop instead of running once), `--interval`
  (seconds, default 300, only meaningful with `--watch`), `--path` (default
  `.`). `--watch` catches `Ctrl+C` and prints a clean "stopping" message.

### Running it

```bash
codescan init          # recreate test1/test1cloud if they predate project_key
codescan run --config test1
```

Expected output, in order: `Running sonar-scanner against . ...` (the
scanner's own output isn't streamed live - it's captured and only shown in
full if the scanner exits non-zero) -> `sonar-scanner finished; waiting for
SonarQube analysis task <ceTaskId> ...` -> `Analysis finished - triggering
ScanToTicketWorkflow ...` -> a summary:

```
Done: created 2 ticket(s), skipped 0 already-ticketed finding(s) (SonarQube task <ceTaskId>)
  created SONAR-3 for finding AbCdEfGh...
  created SONAR-4 for finding ZyXwVuTs...
```

**`--watch` example:**

```bash
codescan run --config test1 --watch --interval 600
```

Runs the same cycle every 10 minutes until `Ctrl+C`, printing a fresh
summary each time.

### Confirming dedupe still holds through direct invocation

Exactly like the old webhook path, dedupe is a label
(`source-key-{finding_key}`) checked by `find_existing()` before creating
anything - `codescan run` doesn't change that logic, only how the
workflow gets triggered. To confirm:

1. Run `codescan run --config test1` once against a project with at least
   one flagged issue - note the `created` count and ticket key(s).
2. Run it again, unchanged, against the same code:
   ```bash
   codescan run --config test1
   ```
   This submits a *new* SonarQube analysis (a new `ceTaskId`, so a new
   `sonar-jira-{ceTaskId}` workflow), but `create_tickets_activity` calls
   `find_existing()` per finding before creating anything - the same
   `source-key-{finding_key}` labels from step 1 are still there.
3. Confirm the second run's summary shows `created 0` and `skipped N`
   matching step 1's `created` count, and that Jira still shows exactly
   one ticket per finding (no duplicates).

## Prerequisites

- Docker (for SonarQube Community Build)
- Python 3.10+
- The [Temporal CLI](https://docs.temporal.io/cli) (`temporal` command) for
  running the local dev server

## 1. Start SonarQube (Docker)

```bash
docker run -d --name sonarqube -p 9000:9000 sonarqube:community
```

Wait a minute or two for it to start, then open http://localhost:9000
(default login: `admin` / `admin`, you'll be asked to change the password).

## 2. Generate a SonarQube token

In SonarQube: **My Account > Security > Generate Tokens**. When picking a
token type, use **User Token**, not "Project Analysis Token" - a
project-scoped token (prefix `sqp_`) can only analyze the one project it
was issued for and can't auto-create new projects, which will fail with
`You're not authorized to analyze this project or the project doesn't
exist...` the first time you scan a new project key. A user token
(prefix `squ_`) tied to your admin account has full rights. Copy it,
you'll put it in `.env` as `SONAR_TOKEN`.

## 3. Scan this repo

Install the [SonarScanner CLI](https://docs.sonarsource.com/sonarqube/latest/analyzing-source-code/scanners/sonarscanner/)
locally, then from the repo root:

```bash
sonar-scanner -Dsonar.token=<your-token>
```

This uses the `sonar-project.properties` at the repo root (project key
`sonar-to-jira`, `.venv`/`__pycache__`/`.git`/`.ruff_cache` excluded) and
scans the actual pipeline code (`receiver/`, `temporal/`, `core/`,
`scanner/`, `ticket/`). After it finishes, refresh the SonarQube UI - you should see
the project with a handful of flagged issues (e.g. CSRF disabled on the
webhook route, the Flask debugger left on).

Note: the scanner must be run from the directory containing
`sonar-project.properties`, or it won't find it (logs `Project root
configuration file: NONE`) and will silently fall back to trying to talk
to SonarCloud instead of your local instance.

Project keys are effectively case-insensitive for uniqueness purposes -
if you ever hit `Could not create Project with key: "x". A similar key
already exists: "X"`, an earlier attempt already created a differently-cased
version of that project. List existing projects with
`curl -u "$SONAR_TOKEN:" "http://localhost:9000/api/projects/search"` and
either delete the stray one (**Project Settings > Deletion**, or
`POST /api/projects/delete?project=<key>`) or match its exact casing in
your `sonar-project.properties`.

## 4. Configure the webhook

In SonarQube: go to your project (or **Administration > Webhooks** for a
global one) and add a webhook:

- **URL:** `http://host.docker.internal:5001/webhooks/sonarqube`
  (use this instead of `localhost` since SonarQube runs inside Docker and
  needs to reach your host machine - on Linux you may need
  `http://172.17.0.1:5001/webhooks/sonarqube` instead, or run SonarQube
  with `--network host`)
- **Secret:** any string you like - put the same value in `.env` as
  `SONAR_WEBHOOK_SECRET`

Webhooks are a standard feature of SonarQube Community Build - they are
**not** a paid/Enterprise feature, despite how it might look if you
stumble on an unrelated locked feature nearby (PR decoration, branch
analysis, and ALM bindings are the things actually gated behind
Developer/Enterprise editions).

Be careful pasting the secret into the SonarQube UI field - if you
copy-paste it including surrounding quotes from `.env`, the webhook will
sign with a different string than what your receiver expects, and every
delivery will get rejected with 401. You can check what SonarQube thinks
the delivery status was (without ever seeing the secret itself, which is
write-only) via:

```bash
curl -u "$SONAR_TOKEN:" "http://localhost:9000/api/webhooks/deliveries"
```

If `success` is `false` with `httpStatus: 401` even though your receiver
logs show the secret loaded correctly, re-set the webhook secret from the
API to guarantee an exact match (replace the webhook UUID with your own,
from `/api/webhooks/list`):

```bash
curl -u "$SONAR_TOKEN:" -X POST "http://localhost:9000/api/webhooks/update" \
  --data-urlencode "webhook=<webhook-uuid>" \
  --data-urlencode "name=sonar-to-jira" \
  --data-urlencode "url=http://host.docker.internal:5001/webhooks/sonarqube" \
  --data-urlencode "secret=$SONAR_WEBHOOK_SECRET"
```

## 5. Set up Jira Cloud

1. Sign up for a free Jira Cloud site at https://www.atlassian.com/software/jira/free
   if you don't have one, and create (or pick) a project. Note its
   **project key** (e.g. `SONAR`) - it's shown next to the project name in
   the project settings, and used as a prefix on every issue in that
   project (`SONAR-1`, `SONAR-2`, ...).
2. Generate an API token: go to
   https://id.atlassian.com/manage-profile/security/api-tokens ->
   **Create API token**. Copy it immediately, you can't view it again.
3. Note the email address of the Atlassian account the token belongs to -
   that's `JIRA_EMAIL`.
4. Your `JIRA_URL` is your site's base URL, e.g.
   `https://your-domain.atlassian.net`.

You'll put all four of these (`JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`,
`JIRA_PROJECT_KEY`) in `.env` in the next step.

**If your project is "team-managed" (the default template for new free
Jira sites, e.g. a Scrum project), it may not have a `Bug` issue type out
of the box** - only `Epic`, `Task`, `Story`, `Subtask`. This project
creates tickets with `"issuetype": {"name": "Bug"}`, so if that type
doesn't exist you'll get `400 Specify a valid issue type` on ticket
creation. Fix: **Project settings > Issue types > Add issue type**,
name it exactly `Bug`. (This is different from the "Features" toggle
some Jira docs mention - in current team-managed projects, issue types
are added directly on this page, not toggled as a feature.) You can check
what issue types exist for your project via:

```bash
curl -u "$JIRA_EMAIL:$JIRA_API_TOKEN" \
  "$JIRA_URL/rest/api/3/issue/createmeta?projectKeys=$JIRA_PROJECT_KEY&expand=projects.issuetypes"
```

**New tickets land in the active sprint automatically, if one exists.**
`ticket/client.py`'s `JiraClient` looks up the project's (first) Agile board and its
active sprint, and moves each newly-created ticket into it. If there's no
active sprint running, tickets fall back to the backlog (not an error,
just a log warning) - start a sprint on your board if you want to see
tickets show up on the Board view rather than the Backlog view.

**Why dedupe uses a label instead of a custom field:** a label is a
built-in field every Jira project already has - no setup, no
permissions, works immediately on a brand-new free-tier project. A
custom field would need to be created and explicitly added to the
project's screen/permission scheme before the API could read or write it,
which is extra one-time admin work per project. For a personal POC, a
label (`source-key-{finding_key}`) gets the same dedupe behavior for
zero setup cost.

## 6. Set up the Python environment

```bash
uv sync
uv run playwright install chromium
cp .env.example .env
```

`uv sync` creates/updates `.venv` from `pyproject.toml`/`uv.lock` (see
Phase 2's packaging section above). `playwright install chromium`
downloads Playwright's own browser binary (used by `scanner/screenshot.py`
to screenshot flagged findings) - it's separate from the pip package and
only needs to run once.

Edit `.env` and fill in:

- `SONAR_WEBHOOK_SECRET` - matches the secret you set on the webhook
- `SONAR_TOKEN` - the token from step 2
- Leave `SCANNER_TYPE=sonarqube` and `SONAR_HOST_URL=http://localhost:9000` as-is
- Leave `TICKET_BACKEND=jira` as-is
- `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY` - from step 5

Because `.env` isn't loaded automatically by these scripts, load it into
your shell before running anything. Use `source`, not `export $(... | xargs)`
— the latter doesn't strip quotes from values, so a value written as
`FOO="bar"` ends up in the environment as the literal string `"bar"`
(quotes included), which breaks every API call that uses it:

```bash
set -a
source .env
set +a
```

Run this in every terminal (worker, receiver) before starting the
corresponding process, and re-run it (then restart the process) any time
you edit `.env`. Note that re-sourcing `.env` in a terminal does **not**
update an already-running process's environment - you must actually kill
and restart the worker/receiver process for the new values to take effect.
You can sanity-check what a running process actually has loaded with (on
macOS):

```bash
ps eww -p <pid> | tr ' ' '\n' | grep SONAR_WEBHOOK_SECRET
```

## 7. Start the Temporal dev server

In its own terminal:

```bash
temporal server start-dev
```

This starts Temporal on `localhost:7233` and a Web UI at
http://localhost:8233 where you can watch workflow executions.

## 8. Start the Temporal worker

In another terminal (with the venv activated and `.env` sourced). Run it
as a module from the repo root, not as a script - the package imports
(`from temporal.activities... import ...`, `from core.models import ...`) only
resolve when the repo root is on `sys.path`, which `-m` gives you
automatically:

```bash
python -m temporal.worker
```

## 9. Start the webhook receiver

In another terminal (same venv/env), also run as a module from the repo
root:

```bash
python -m receiver.app
```

This starts Flask on `http://localhost:5001`.

## 10. Trigger it

Re-run the scanner from the repo root (or click **"Trigger webhook"**
manually if SonarQube's admin UI exposes it, depending on version):

```bash
sonar-scanner -Dsonar.token=<your-token>
```

When the analysis finishes, SonarQube fires the webhook -> `receiver/app.py`
verifies the signature -> starts `ScanToTicketWorkflow` -> the workflow
calls `fetch_findings_activity` (hits SonarQube via
`SonarQubeServerClient`) -> then `create_tickets_activity` (hits
Jira via `JiraClient`, checking `find_existing` before creating
anything) -> results are logged.

### How to confirm success

1. **Temporal Web UI** (http://localhost:8233) - find the workflow run and
   confirm it **Completed**. There are three activities in the run:
   `fetch_findings_activity`, `create_tickets_activity` (should show a
   `created` list with one entry per flagged finding and an empty `skipped`
   list on a first run), and one `capture_and_attach_screenshot_activity`
   per created ticket. A failure on the screenshot activity (visible
   per-activity in the UI) does not turn the overall workflow result into
   a failure.
2. **worker terminal** (`python -m temporal.worker`) - look for lines like:
   ```
   Tickets: created 1 ticket(s), skipped 0 already-ticketed finding(s)
     created SONAR-1 for finding AbCdEfGh...
     attached screenshot to SONAR-1
   ```
3. **Jira** - open your project in Jira Cloud, confirm new ticket(s)
   appeared with a descriptive summary, type `Bug`, and
   labels including `sonarqube`, `security`, and a
   `source-key-<finding-key>` label. If your project has an **active
   sprint**, the ticket lands directly on the **Board** tab; if not, it's
   in the **Backlog** tab instead (new tickets never appear on the board
   without an active sprint to put them in). Also confirm the ticket has a
   `sonarqube-{finding-key}.png` attachment showing the flagged code.

### Verifying dedupe (no duplicate tickets on a re-scan)

1. With tickets already created from step 10 above, re-run the exact same
   scan without changing any code:
   ```bash
   sonar-scanner -Dsonar.token=<your-token>
   ```
   SonarQube re-analyzes the code, finds the same issues (same stable
   issue keys, since nothing changed), and fires the webhook again with a
   new `taskId`.
2. Watch the worker terminal for the second run. You should now see
   something like:
   ```
   Tickets: created 0 ticket(s), skipped 2 already-ticketed finding(s)
     skipped finding AbCdEfGh... (ticket already exists)
     skipped finding ZyXwVuTs... (ticket already exists)
   ```
   The `skipped` count should match the number of findings that had tickets
   from the previous run, and `created` should be empty (assuming no new
   findings were introduced).
3. Cross-check in Temporal Web UI: open the second workflow execution's
   result and confirm `created` is `[]` and `skipped` contains the same
   finding keys from the first run.
4. Cross-check in Jira: refresh the project board and confirm there's
   still exactly one ticket per `source-key-<key>` label - no duplicates.

If you want to force a *new* ticket to prove dedupe isn't just "always
skip," introduce a new flagged issue somewhere in the codebase, re-scan,
and confirm exactly one new ticket is created for it while the existing
issues are still reported as skipped.

## Running the unit tests

The `tests/` suite is fully mocked (no SonarQube/Jira/Temporal server
needed) - it covers `capture_finding_screenshot`'s text extraction and
`capture_and_attach_screenshot_activity`'s dispatch/isolation logic:

```bash
uv sync   # includes pytest + pytest-asyncio
uv run pytest
```

`pytest.ini` sets `asyncio_mode = auto` so `async def test_...` functions
run without per-test `@pytest.mark.asyncio` decorators.

## Testing the receiver without a real SonarQube webhook

You can hand-craft a signed request to test the receiver in isolation:

```python
import hmac, hashlib, json, requests

secret = "changeme"  # must match SONAR_WEBHOOK_SECRET
body = json.dumps({
    "taskId": "test-task-1",
    "project": {"key": "sonar-to-jira"},
}).encode()

signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

requests.post(
    "http://localhost:5001/webhooks/sonarqube",
    data=body,
    headers={
        "Content-Type": "application/json",
        "X-Sonar-Webhook-HMAC-SHA256": signature,
    },
)
```

(`receiver/app.py` needs to already be running for this to hit anything.)

## Project structure

```
receiver/
  app.py                       Flask app: POST /webhooks/sonarqube route only
  verify_signature.py          HMAC-SHA256 webhook signature verification
  starter.py                   Connects to Temporal and starts ScanToTicketWorkflow

temporal/
  worker.py                    Temporal worker entrypoint (task queue: sonar-jira-queue)
  data_converter.py            TASK_QUEUE constant + the Pydantic-aware data converter
  activities/
    fetch_findings.py          fetch_findings_activity (calls the ScannerClient interface,
                                stamps each Finding.branch from the worker's local git state)
    create_tickets.py          create_tickets_activity (dedupe + create via the TicketClient interface)
    capture_and_attach_screenshot.py  capture_and_attach_screenshot_activity (screenshot + attach +
                                extracted-text comment, bonus capability)
  workflows/
    scan_to_ticket.py          ScanToTicketWorkflow
  models/
    sonar_to_jira.py           SonarToJiraInput (the workflow's input model)
    screenshot_attach.py       ScreenshotAttachInput (the screenshot activity's input model)

core/
  models.py                    Finding (incl. branch), Severity (normalized vocabulary),
                                CreatedTicket, TicketResult

scanner/
  client.py                    ScannerClient (ABC) - the scanner extension point.
                                SonarQubeServerClient (real) + SonarQubeCloudClient (stub) +
                                get_scanner_client() (picks one based on SCANNER_TYPE)
  screenshot.py                 capture_finding_screenshot (Playwright, async API) - bonus capability,
                                returns a FindingExtraction (screenshot path + best-effort
                                code snippet/annotation text)

ticket/
  client.py                    TicketClient (ABC) - the ticket extension point.
                                JiraClient (+ its attach_screenshot and add_comment bonus methods) +
                                get_ticket_client() (picks one based on TICKET_BACKEND)

tests/
  scanner/test_screenshot.py                            capture_finding_screenshot extraction tests
  temporal/activities/test_capture_and_attach_screenshot.py  activity dispatch/isolation tests

cli/
  main.py                        codescan typer app - registers init (run/down are stubs)
  init_wizard.py                 the interactive init flow (see Phase 2 above)
  prerequisites.py               check_java()/ensure_java() - portable JRE into ~/.codescan/jre/
  help_links.py                  HELP_LINKS doc-URL dict + print_help_link()

crypto_utils.py                  encrypt_token()/decrypt_token() (Fernet, keyed by FERNET_KEY)
config_store.py                  configs/config_credentials CRUD (see Phase 1 above)
sql/init.sql                     configs + config_credentials schema
scripts/
  bootstrap_env.py               .env field-by-field merge, port probing, FERNET_KEY safety check
  seed_test_config.py            throwaway config_store round-trip verification script

Dockerfile                       containerizes temporal/worker.py, built via uv
docker-compose.yml               postgres/temporal/worker (always-on) + sonarqube (profile-gated)
pyproject.toml, uv.lock          dependencies + the codescan console script (see Phase 2 above)
pytest.ini                     asyncio_mode = auto, for the async activity/screenshot tests
.env.example
sonar-project.properties       Scan config for this repo (project key: sonar-to-jira)
```

Every top-level package (`receiver/`, `temporal/`, `core/`, `scanner/`,
`ticket/`) has an `__init__.py`, and the two entrypoints
(`temporal/worker.py`, `receiver/app.py`) must be run with `python -m`
from the repo root so their `from core...`/`from scanner...`/
`from ticket...`/`from temporal...` imports resolve - see steps 8 and 9.

## What's not built yet

- `SonarQubeCloudClient` real implementation
- Updating/transitioning existing Jira tickets when a Sonar issue is
  resolved (currently only create + dedupe-skip; no closing logic)
