# gozu — the complete guide

This document explains **everything** about this codebase as it stands
right now: what the product is, exactly what happens when you use it, how
every module works, how the GitHub Actions pipelines fit in, and where the
other docs in this repo (`README.md`, `ARCHITECTURE.md`, `CLAUDE.md`) are
stale relative to the current code. It's written to stand alone — you
shouldn't need to open the source to follow it.

> **A note on the other docs in this repo**: `ARCHITECTURE.md` describes
> an early, pre-CLI phase of this project and is now badly out of date.
> `README.md` is largely accurate and already covers several of the
> features below (backlog cap, auto-closing resolved findings, `gozu
> config`) — this document goes deeper and adds what README doesn't cover
> yet (short flag aliases, `--version`, `gozu up --config` scoping,
> graceful-interrupt handling, the config-not-found recovery picker).
> `CLAUDE.md`'s own CLI-conventions section is stale in one specific way:
> it still describes `cli/stack.py` as a single file — it's been a
> package (`cli/stack/`) for a while now, and several newer modules
> (`cli/wizard_engine.py`, `cli/config_cmd/`, `cli/config_lookup.py`,
> `cli/status.py`, `core/errors.py`) aren't mentioned there at all.

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
4. **Auto-closes tickets** whose underlying SonarQube finding gets
   resolved, and caps how many new tickets one run can create, rolling
   the rest into a single shared "backlog" ticket instead of flooding
   Jira.

The whole pipeline is orchestrated by **Temporal** (a workflow engine), so
that scanning → deduping → ticket-creation → screenshotting → reconciling
survives crashes and retries instead of being one long fragile Python
script.

Everything runs locally via **Docker Compose**: Postgres (stores your saved
scanner/Jira credentials, encrypted, plus the dedupe/reconciliation
ledger), Temporal (the workflow engine), and optionally a local SonarQube
container. Jira is the only piece that must be a real external service
(Cloud or self-hosted). There's also now a **fourth**, entirely separate
way to run this — a standalone GitHub Action needing no local
infrastructure at all (§9).

---

## 2. The four ways a scan can happen

You choose one of the first three **per saved config**, during `gozu init`:

| Mode | Used for | Mechanics |
|---|---|---|
| `direct` | Self-hosted SonarQube, on-demand | `gozu run` shells out to `sonar-scanner` itself, waits for SonarQube's server-side analysis to finish, then triggers the ticket-creation workflow. |
| `watch` | SonarQube Cloud **Free** plan | Free only re-analyzes a PR *after* it merges to main — there's no webhook to receive on that plan, so `gozu run --watch` just re-runs the `direct` cycle on a timer (default every 300s). |
| `webhook` | Self-hosted SonarQube, or SonarQube Cloud **Premium** | SonarQube itself calls back into this tool's Flask receiver the moment its analysis finishes. Premium can track several branches (with glob patterns like `release/*`); each incoming webhook is checked against that list. |
| *(GitHub Action)* | SonarQube Cloud Free, `main`-only | An entirely separate pipeline (§9) — a GitHub Actions workflow reacting to SonarCloud's own Automatic Analysis on pushes to `main`. Not a saved config's trigger mode; it's a standalone workflow file with its own credentials via GitHub Secrets. |

A config tracking more than one branch and run via `gozu run` directly (not
webhook) fans out into **one child Temporal workflow per branch** under a
parent workflow (`MultiBranchScanWorkflow`). A webhook delivery only ever
concerns one branch, so it never needs to fan out. Branch tagging itself
is **Premium-only**: self-hosted Community Build rejects the
`sonar.branch.name` scanner property outright, and Cloud's Free plan
rejects *querying* issue data for anything but `main` at the API level
("Organization is not allowed to access data from non main branches" —
confirmed live) — so `run_scan_cycle` only computes a real branch when
`config["sonar_plan"] == "premium"`; every other case scans untagged.

---

## 3. Full walkthrough: `gozu run` (direct mode)

1. **`gozu run [-c/--config NAME] [-w/--watch] [-i/--interval N] [-p/--path .]`**
   (`cli/main.py`) loads `.env` into the process first (`_load_env()`, a
   `@app.callback()` that runs before every command).
2. `select_config(name)` (`cli/scan_runner/config_select.py`) resolves
   which saved config to use. If a name *was* given but doesn't match any
   saved config, it now delegates to `resolve_config_or_prompt()`
   (`cli/config_lookup.py`, §5) — a shared recovery picker that shows every
   saved config plus an "Exit" choice, rather than just erroring out. If
   no name was given, the original auto-pick/`questionary` picker logic
   still applies (auto-pick if exactly one config exists, a picker if
   several, a clean error if none).
3. `run_scan_cycle(config, path)` (`cli/scan_runner/__init__.py`) —
   unchanged in shape from before: `ensure_java()`/`ensure_sonar_scanner()`
   → compute branch (Premium-only, see §2) → `run_scanner()` → 
   `read_ce_task_id()` → `wait_for_analysis()` → `trigger_workflow()` →
   returns a summary dict (`ce_task_id`, `created`, `skipped`).
4. `_print_summary()` (`cli/main.py`) prints created/skipped counts and
   one line per ticket.

With `--watch [--interval N]` (default 300s), this loops on
`time.sleep(interval)` until `Ctrl+C` (caught cleanly).

---

## 4. What the Temporal workflow does (`ScanToTicketWorkflow`)

This is the shared core, run by direct/watch/webhook trigger paths
(`temporal/workflows/scan_to_ticket.py`). It now has **four** steps, not
three:

1. **`fetch_findings_activity`** — asks the right `ScannerClient` to fetch
   normalized `Finding`s. Retried generously (`initial_interval=2s`,
   `maximum_attempts=8`) because SonarQube's search index can lag a few
   seconds behind a compute-engine task's own `SUCCESS`.
   `non_retryable_error_types=["ScannerAuthError"]` — a bad/expired token
   is a permanent failure, not worth retrying (§7).
2. **`create_tickets_activity`** — for each finding, dedupe via the
   `ticket_claims` ledger, create a Jira ticket if new. Now capped at 30
   new tickets per run, with the rest deferred into a single shared
   "backlog" ticket (§8). `non_retryable_error_types=["TicketAuthError",
   "TicketValidationError"]`, `maximum_attempts=3`.
3. **`reconcile_resolved_findings_activity`** (new) — checks every
   currently-open ticket this destination is tracking: has its ticket been
   deleted out-of-band in Jira? Has SonarQube marked the underlying
   finding resolved? Closes tickets accordingly (§8). Runs on **every**
   scan cycle, for every config, with no opt-out. Wrapped in its own
   `try/except` in the workflow so a reconciliation failure can never
   undo or block the ticket-creation work that already succeeded this
   run. `maximum_attempts=3`, 60s timeout.
4. **If any tickets were created**, fans out one
   `capture_and_attach_screenshot_activity` per new ticket, concurrently,
   with the same failure-tolerance as before (`return_exceptions=True`,
   `maximum_attempts=2`) — unchanged from prior behavior.

Returns a `TicketResult` (`created`, `skipped`, and now **`deferred`** —
findings that were new but didn't get a ticket this run because the
30-cap was hit; distinct from `skipped`, which means "already ticketed").

`MultiBranchScanWorkflow` fans out one child `ScanToTicketWorkflow` per
branch and merges `created`/`skipped` — **`deferred` is not aggregated
into the parent's result** (each child still creates/updates its own
rollup ticket independently; the parent's own summary just doesn't
surface the combined deferred count).

---

## 5. CLI surface — what's new here

The CLI grew substantially since the config-store/CLI rewrite. `cli/main.py`
is `no_args_is_help=True` and gained:

- **`--version`** — an eager callback reading `importlib.metadata.version("gozu")`
  (falls back to a friendly "not installed as a package" message if run
  from a raw checkout without installation).
- **Short flag aliases** on `gozu run`: `-c/--config`, `-w/--watch`,
  `-i/--interval`, `-p/--path`.
- **`gozu status`** — a standalone health check (`cli/stack/status_report.py`
  + `cli/stack/__init__.py:status()`). Deliberately makes **no** attempt to
  bring anything up first (no `ensure_postgres_up()` etc.) — it's meant to
  be safe to run at any time, even before `gozu init`, and checks every
  possible service (`ALL_PROFILES`) rather than reading configs from a
  Postgres that might itself be down.
- **`gozu ports`** — reads `~/.gozu/stack/.env` and prints each configured
  port with a human label.
- **`gozu up --config/-c NAME`** — scope profile activation to just one
  saved config instead of the aggregate of every config (the old,
  still-available default behavior when `--config` is omitted).
- **`gozu config list` / `gozu config edit <name>` / `gozu config delete <name>`**
  — full config management, previously only possible by re-running
  `gozu init` or touching Postgres directly. Covered in §6.
- **Plain, portable Unicode status symbols** (`cli/status.py`:
  `✓`/`✗`/`⚠`/`…`, no emoji, no variation selectors) used consistently
  across the wizard, prerequisites, and stack commands — replaces earlier
  ad hoc emoji.
- **Graceful interrupt handling** (`cli/stack/cleanup.py`'s
  `InterruptCleanup`, used by `gozu init`/`up`/`down`, not `gozu run`
  which never touches Docker) — registers `SIGINT`/`SIGTERM` handlers (plus
  a `KeyboardInterrupt` catch as a fallback) so a `Ctrl+C` mid-`gozu up`
  actually terminates the underlying `docker compose` child process and
  cleans up only whatever *this invocation* started — never a
  pre-existing service or an unrelated stack.
- **Prerequisites now run upfront** — `cli/init_wizard/env_step.py`'s
  prerequisite step now runs unconditionally during `gozu init` for both
  Local and Cloud (both scan via `sonar-scanner` on the host either way),
  so a first `gozu run` is never surprised by a Java/sonar-scanner
  download mid-scan; `run_scan_cycle`'s own calls to `ensure_java()`/
  `ensure_sonar_scanner()` remain as an idempotent defensive fallback.

### The shared wizard engine (`cli/wizard_engine.py`)

Both `gozu init` and `gozu config edit` now go through the same engine
instead of two separate, drifting prompt flows:

- `WizardField(key, label, prompt: Callable[[], str], secret: bool = False)`
  — one dataclass per editable field.
- `run_wizard(fields, initial_state, walk_first: bool) -> dict` — if
  `walk_first=True` (used by `gozu init`), walks every field's `prompt()`
  once up front; either way, then loops a `questionary.select` "review
  screen" (each field's current value, masked if `secret`, plus a "Looks
  good — save" choice) until the user confirms, re-running just the
  selected field's prompt on each edit. **Mutates and returns the same
  `initial_state` dict** — deliberate, since every field's `prompt()`
  closure is built over that same mutable dict, so an edit made via the
  engine is immediately visible to every other field's `default=`.

`cli/prompts.py` (moved out of `cli/init_wizard/`, otherwise unchanged)
exists at the top level of `cli/` specifically to avoid a circular import:
`cli/wizard_engine.py` needs `ask_or_exit()`, and importing a submodule of
`cli.init_wizard` would trigger `cli/init_wizard/__init__.py`, which
itself imports `cli.wizard_engine`.

### The config-not-found recovery picker (`cli/config_lookup.py`)

`resolve_config_or_prompt(name) -> dict`: on a hit, returns
`config_store.get_config(name)` unchanged. On a miss, instead of just
erroring, shows every saved config in a `questionary.select` (plus an
"Exit" choice — using a sentinel value rather than `None`, specifically
because `questionary.Choice` defaults a `None` value to its own title
string, which would silently produce a bogus config literally named
`"Exit"`). Used by `gozu run --config`, `gozu up --config`, and both
`gozu config edit`/`delete`.

---

## 6. Managing saved configs (`gozu config`)

`cli/config_cmd/`:

- **`gozu config list`** — every config's name, scanner type/mode, trigger
  mode. No secrets (mirrors `config_store.list_configs()`).
- **`gozu config delete <name>`** — resolves via the recovery picker,
  prints exactly what will be removed (including whether its Jira
  credentials are embedded or point at a shared ticket destination), one
  `questionary.confirm`, then `config_store.delete_config(name)`.
- **`gozu config edit <name>`** (`cli/config_cmd/edit.py`) — builds a
  `WizardField` list from the config's *current* editable fields (never
  the structural ones — `scanner_type`/`scanner_mode`/`sonar_plan`/
  `trigger_mode` are fixed at `gozu init` and never offered here) and runs
  it through `run_wizard(..., walk_first=False)`, then diffs the result
  against a pre-edit snapshot to find exactly what changed:
  - `project_key`/`branches` are plain `configs` columns — one
    `config_store.update_config_fields(name, **updates)` call.
  - Every other changed field (`sonar_token`, `sonar_host_url`/
    `sonar_organization`, `webhook_secret`, or any of the four Jira
    fields) goes through `config_store.update_config_credential(name, key, value)`,
    which writes to wherever that key actually lives — this config's own
    `config_credentials`, or, for the four Jira fields
    (`jira_url`/`jira_email`/`jira_api_token`/`jira_project_key`), the
    shared `ticket_destinations`/`ticket_destination_credentials` row if
    this config uses one (mirroring `get_config()`'s own read-side
    resolution logic).
  - **Editing a shared destination's Jira credentials warns you first**:
    if the changed field is one of the four Jira fields and this config
    references a shared destination, `count_configs_using_destination()`
    checks how many *other* configs share it; if nonzero, a second
    confirmation is required before writing, since the edit will affect
    every config sharing that destination, not just this one.

---

## 7. Error handling: `core/errors.py` and Temporal's retry matching

A new, small exception hierarchy exists purely to make Temporal's
`RetryPolicy(non_retryable_error_types=[...])` actually work correctly —
and the reasoning here is genuinely subtle, confirmed directly against the
installed `temporalio` SDK (1.32.0) rather than assumed:

- `GozuError(Exception)` — a shared base, but **organizational only**
  (useful for `isinstance` checks elsewhere in the code).
- `ScannerAuthError(GozuError)` — a 401/403 from the scanner backend.
- `TicketAuthError(GozuError)` / `TicketValidationError(GozuError)` — a
  401/403 or 400 from the ticket backend, respectively.

**The subtlety**: Temporal's retry logic matches
`non_retryable_error_types` against the raised exception's class name via
plain, case-insensitive **string equality** — not `isinstance`, not MRO
traversal. A failure's recorded `type` is always the exception's exact,
most-derived class name (`exception.__class__.__name__`). That means
`non_retryable_error_types=["GozuError"]` would **only** ever match a
literal `GozuError` instance — it would never match a `ScannerAuthError`
raised in practice, even though `ScannerAuthError` *is a* `GozuError` in
Python's own type system. This is why every concrete exception is listed
by name in each activity's own `RetryPolicy`
(`temporal/workflows/scan_to_ticket.py`) instead of relying on the shared
base class once.

`scanner/sonarqube_common.py`'s `_fetch_all_pages()` raises
`ScannerAuthError` on a 401/403 response; `ticket/jira_client.py`'s
`_raise_for_status()` raises `TicketAuthError` (401/403) or
`TicketValidationError` (400), falling back to a plain `RuntimeError` for
anything else.

---

## 8. Ticket lifecycle: backlog cap, rollup ticket, and auto-close

This is the single biggest functional addition since the last pass — gozu
now manages a ticket's *whole* life, not just its creation.

### The 30-ticket backlog cap and rollup ticket

`temporal/activities/create_tickets.py`'s `BACKLOG_CAP = 30` (a plain
module constant, not user-configurable via any input model) limits how
many **new** tickets a single run creates. Once that cap is hit within a
run, every further new finding is added to `TicketResult.deferred` without
being claimed — so a future run reconsiders it completely fresh, not
"stuck" behind a partial claim.

After the per-finding loop, `upsert_rollup_ticket()` (a bonus method,
`getattr`-dispatched, on `JiraClient`) is called with exactly the deferred
findings — **every run, even when `deferred` is empty**, so an existing
rollup ticket's count can shrink back down to zero as the backlog clears.
It finds-or-creates-or-updates one ticket labeled `gozu-backlog-rollup`,
capped at 50 displayed lines with a "... and N more" summary if there are
more findings than that.

### Auto-closing resolved findings (`reconcile_resolved_findings_activity`)

New activity (`temporal/activities/reconcile_resolved_findings.py`),
`ReconcileResolvedFindingsInput` (new model) deliberately carries **no**
`project_key`/`branch` — the `ticket_claims` ledger is already scoped
per-destination, and resolution-checking looks up specific finding keys
directly rather than a project's full list. Runs inline inside
`ScanToTicketWorkflow`, on every cycle, for every config — no separate
Temporal Schedule/cron. Logic, all inside one shared Postgres connection:

1. `claims.list_open()` — every currently-tracked, still-open ticket for
   this destination. Empty → return immediately.
2. **If the ticket backend supports `ticket_exists()`**, check *every*
   open claim's existence up front — independent of whether SonarQube has
   marked anything resolved. This fixes a real bug: the previous version
   checked resolutions *first* and returned early when nothing was
   resolved, meaning a ticket deleted out-of-band in Jira for a finding
   that's still genuinely open in SonarQube was never even looked at. A
   confirmed-gone ticket clears its claim (`claims.clear_stale()`) and
   drops out of the working set. Each existence check is wrapped in its
   own `try/except` — an error checking is treated conservatively as
   "still exists" rather than risking clearing a good claim on a
   transient failure.
3. If the ticket backend doesn't support `transition_to_done()`, warn and
   stop — nothing more to do without a way to actually close anything.
4. `scanner_client.fetch_resolutions(finding_keys)` (`ScannerClient`'s
   third abstract method) — batch-checks exactly the still-open finding
   keys against SonarQube's current state; a key absent from the result
   is simply still open, never mapped to `None`.
5. For each now-resolved finding: `transition_to_done(ticket_key)` walks
   Jira's actually-available transitions for one whose target status
   category is `"done"` (never hardcodes a status name like "Done", since
   workflow names vary per project) — `False` means no such transition
   exists, logged and skipped, not treated as an error. On success, an
   optional comment is posted ("Closed automatically — SonarQube marked
   this {FIXED/REMOVED/WONTFIX/FALSE-POSITIVE}") and the claim is marked
   closed (`claims.mark_closed()`).
   **Each claim's entire auto-close attempt is individually wrapped in
   its own `try/except`** — a single claim raising (e.g. a 404 transitioning
   an already-deleted ticket) used to propagate out of the whole activity
   and silently skip reconciling every other claim in that run; now it's
   isolated, matching the same "one bad item can't sink the batch"
   principle already used for sprint assignment and the rollup ticket.

### The claims ledger, updated (`ticket/claims.py`)

Two real changes beyond what existed before:

1. **One shared Postgres connection per activity invocation**, not one
   per call. Every function's signature now takes an explicit `conn` as
   its first argument (`get_ticket(conn, ...)`, `claim(conn, ...)`,
   `record_ticket(conn, ...)`, `release(conn, ...)`) — the calling
   activity opens exactly one connection and threads it through the whole
   per-finding loop, replacing what used to be up to 3 short-lived
   connections *per finding*.
2. **A `status` column** (`open`/`closed`, migrated via `sql/init.sql`'s
   `ALTER TABLE ticket_claims ADD COLUMN IF NOT EXISTS status TEXT NOT
   NULL DEFAULT 'open'` — every pre-existing row defaults to `open`, never
   retroactively marked closed by the migration itself) backing three new
   functions: `list_open(conn, destination)` (claims with a real
   `ticket_key`, status `open`), `mark_closed(conn, destination,
   finding_key)`, and `clear_stale(conn, destination, finding_key)` — an
   **unconditional** delete (unlike `release()`, which only ever deletes an
   *unfinished*, `ticket_key IS NULL` claim) used specifically when a
   *completed* claim's ticket was confirmed deleted externally.

### Sprint-assignment robustness (`ticket/jira_client.py`, `ticket/jira_sprint.py`)

Two independent hardening fixes:

- **A sprint-assignment failure can no longer undo an already-created
  ticket.** `create_ticket()` now wraps `self._sprints.add_issue(...)` in
  its own `try/except`, logging a warning on failure rather than letting
  the exception propagate and appear to "fail" ticket creation that in
  fact already succeeded.
- **Kanban boards no longer crash ticket creation.** Jira signals "this
  board doesn't support sprints" with an HTTP 400 on the sprint-lookup
  call, not an empty list — `SprintAssigner._active_sprint_id_lazy()` now
  checks for that 400 specifically, before the generic
  `_raise_for_status()` would otherwise turn it into a hard error, and
  just logs "Board doesn't support sprints (Kanban) — new tickets will
  stay in the backlog."

`ticket/base.py`'s `TicketClient` ABC itself gained **no new abstract
methods** for any of this — `attach_screenshot`, `add_comment`,
`transition_to_done`, `upsert_rollup_ticket`, and `ticket_exists` are all
optional, `getattr`-dispatched bonus capabilities, exactly like
`attach_screenshot`/`add_comment` always were.

---

## 9. The standalone GitHub Action

Covered in full detail in this repo's own commit history and comments;
summarized here since it's now part of the "how can a scan happen" answer
(§2). `.github/workflows/sonar-to-jira-main.yml` triggers on `push:
branches: [main]` (`concurrency: {group: sonar-to-jira-main,
cancel-in-progress: false}` so overlapping pushes queue rather than race)
and reacts to SonarQube Cloud's **Automatic Analysis** completing on
`main`, entirely inside GitHub's own runner — no local infrastructure, no
receiver reachable from the internet, only outbound calls to SonarCloud's
and Jira's APIs.

Deliberately scoped to `main` only: SonarQube Cloud Free rejects querying
issue data for any other branch, which rules out a PR-time equivalent.

- Installs via **`pip install .`** (from the checkout the workflow's own
  `actions/checkout@v4` already produced) — **not** a GitHub Release URL.
  This repo is private, and a bare `pip install <release-url>` has no
  credentials to fetch a private repo's release asset (GitHub returns 404,
  not 401/403, for that case — confirmed live, this was the actual first
  real bug hit running this workflow).
- `github_action/main.py`'s `main()`: reads `SONAR_PROJECT_KEY`/
  `COMMIT_TIMESTAMP`/`SONAR_TOKEN` from env, builds a scanner client via
  the existing `get_scanner_client()` legacy env-based factory (no new
  credential-reading code needed), waits for a fresh analysis via
  `SonarQubeCloudClient.wait_for_latest_analysis()` (dispatched via
  `getattr` — a hard `sys.exit(1)` if the configured scanner client
  doesn't support it, e.g. `SCANNER_TYPE` misconfigured to self-hosted,
  since this entrypoint only ever targets Cloud), fetches findings, and
  creates tickets via plain `find_existing()`/`create_ticket()` — **no**
  `ticket_claims` ledger involved at all, since the race that ledger
  exists to close barely applies to a workflow that's inherently
  serialized by `cancel-in-progress: false`. Screenshot capture/comment
  posting mirrors the Temporal activity's body without any Temporal
  context (plain `logging`, same exception `CLAUDE.md` already carves out
  for `receiver/app.py`/`temporal/worker.py`).
- `wait_for_latest_analysis(project_key, branch, not_before, timeout=300,
  poll_interval=5)` (`scanner/sonarqube_cloud.py`) polls
  `api/project_analyses/search` (no `ceTaskId` exists for a scan this
  process didn't trigger itself) until the newest analysis's `date` is at
  or after `not_before` — which is the triggering **commit's own
  timestamp** (`github.event.head_commit.timestamp`), not "now" at the
  job's own start, specifically so the job's own startup time (checkout,
  `pip install`) can't cause it to miss an analysis that finished in
  parallel before the job got around to checking.
- Setup required on the repo before this can succeed (not part of the
  workflow file itself): 7 secrets (`SONAR_TOKEN`, `SONAR_ORGANIZATION`,
  `SONAR_PROJECT_KEY`, `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`,
  `JIRA_PROJECT_KEY`) and a real SonarCloud project with Automatic
  Analysis enabled, matching that org/project key.

---

## 10. Repository layout

```
cli/                       the `gozu` CLI (Typer)
  main.py                    entrypoint: init/run/up/down/status/ports/config/--version
  status.py                   shared plain-Unicode status symbols (✓/✗/⚠/…)
  config_lookup.py            resolve_config_or_prompt() - shared "not found" recovery picker
  wizard_engine.py             shared review/edit engine (WizardField, run_wizard) - init AND config edit
  prompts.py                   ask_or_exit/prompt_text/generate_or_prompt_secret (moved from init_wizard/)
  config_cmd/                  gozu config list/edit/delete
  init_wizard/                 `gozu init` - one file per wizard step, now built on wizard_engine
  scan_runner/                 `gozu run` - scan, poll, trigger workflow
  prerequisites/               download/verify Java + sonar-scanner into ~/.gozu/
  help_links.py                live doc-link lookups shown during prompts
  stack/
    __init__.py                 up/down/status/ports
    cleanup.py                   InterruptCleanup - graceful SIGINT/SIGTERM handling
    status_report.py             print_stack_status() - per-service health symbols
    profiles.py                  active_profiles/service_state/ensure_service_up/stop
    webhooks.py                  webhook secret generation + URL printing
    wipe.py / backup.py          gozu down --wipe + pre-wipe Postgres backup
    files.py                     ensure_stack_files() - materializes the Docker stack for pip installs
    _stackfiles/                 symlinks to the real root-level compose/Dockerfile/sql files

config/                    saved scanner+ticket credential sets ("configs")
  connection.py               shared get_connection() (store.py + ticket_destinations.py - NOT ticket/claims.py, which has its own)
  store.py                     CRUD + update_config_fields/update_config_credential (for `gozu config edit`)
  crypto.py                    Fernet encryption for every stored secret
  ticket_destinations.py       shared Jira-board records + count_configs_using_destination

core/
  models.py                   Finding, Severity, CreatedTicket, TicketResult (created/skipped/deferred)
  errors.py                   GozuError/ScannerAuthError/TicketAuthError/TicketValidationError

scanner/                   scanner backends
  base.py                     ScannerClient ABC: fetch_findings, requirements, fetch_resolutions
  sonarqube_server.py          self-hosted SonarQube
  sonarqube_cloud.py            SonarQube Cloud + wait_for_latest_analysis() (GitHub Action support)
  sonarqube_common.py           shared fetch/paginate/resolve logic (mixin), raises ScannerAuthError
  sonarqube_classify.py         "is this issue security-relevant" logic
  factory.py                    build_scanner_client() / get_scanner_client()
  screenshot.py                 Playwright: screenshot + code extraction

ticket/                    ticket backends
  base.py                      TicketClient ABC (3 required methods; 5 optional bonus methods)
  jira_client.py                Jira implementation: create/find/attach/comment/transition/rollup
  jira_sprint.py                active-sprint lookup, Kanban-board-safe
  adf.py                        Atlassian Document Format node builders
  claims.py                     idempotency ledger - shared conn per activity, open/closed status
  factory.py                    build_ticket_client() / get_ticket_client()

temporal/                  orchestration
  worker.py                     registers all 4 activities + both workflows
  workflows/
    scan_to_ticket.py             fetch -> create -> RECONCILE -> screenshot, with non_retryable policies
    multi_branch_scan.py          per-branch fan-out (deferred not aggregated into parent result)
  activities/
    fetch_findings.py / create_tickets.py / capture_and_attach_screenshot.py
    reconcile_resolved_findings.py   auto-close activity, per-claim isolated
  models/                        one Pydantic model per activity boundary

receiver/                  Flask webhook receiver (unchanged in this pass)
  app.py / verify_signature.py / starter.py

github_action/              standalone GitHub Action entrypoint (no Temporal/Postgres)
  main.py                     wait for Automatic Analysis -> fetch -> create tickets -> screenshot

scripts/                   one-off/bootstrap scripts (paths, bootstrap_env, env_ports, fernet_safety, seed_test_config)
sql/init.sql                Postgres schema (+ ticket_claims.status for auto-close)
tests/                       pytest suite - still only 3 files total (§12)
```

---

## 11. GitHub Actions — the full CI/CD story

Three workflow files now:

- **`.github/workflows/ci.yml`** — runs `pytest` on every push to `main`
  and every PR. Unchanged.
- **`.github/workflows/release.yml`** — tag-triggered (`v*`), version-match
  guard against `pyproject.toml`, builds a wheel/sdist, creates a GitHub
  Release with them attached. Unchanged. Not currently wired to publish
  to PyPI (a prior session explored adding this and deliberately backed
  it out — `pipx install gozu` still isn't possible; installs go through
  a Release URL, or, for private-repo automation, straight from a
  checkout via `pip install .`, as the new Action does).
- **`.github/workflows/sonar-to-jira-main.yml`** (new, §9) — the
  standalone Action.

---

## 12. Testing — what's covered and what isn't

Still, after all of this, exactly **three** test files exist:
`tests/scanner/test_screenshot.py`, `tests/temporal/activities/
test_capture_and_attach_screenshot.py`, and `tests/github_action/
test_main.py`. **None** of the substantial new surface added in this pass
has any test coverage yet: `reconcile_resolved_findings_activity`'s
per-claim isolation, the 30-ticket cap/rollup-ticket logic, `cli/
wizard_engine.py`, `cli/config_cmd/`, `cli/stack/cleanup.py`'s interrupt
handling, `core/errors.py`'s retry-matching behavior, or the Kanban-board/
sprint-failure-tolerance fixes in `ticket/jira_client.py`.

(There's also harmless leftover debris worth knowing about, not part of
the current source: stale compiled bytecode under `jira/__pycache__/`,
`sonar/__pycache__/`, and `tests/sonar/__pycache__/` for modules that no
longer exist as `.py` files anywhere — left over from before the
`sonar/`→`scanner/`, `jira/`→`ticket/` rename. Functionally inert, safe to
delete, not worth treating as real source.)

---

## 13. Known limitations (still true today)

- **SonarQube Cloud's Free plan has no push mechanism** for anything but
  `main` — `watch` mode is a polling workaround; the GitHub Action (§9)
  is the only event-driven option, and only for `main`.
- **No branch-scoped querying on Free** at all, PR or otherwise — this is
  a hard SonarQube Cloud API restriction, not something gozu's code
  controls.
- **`reconcile_resolved_findings_activity` has no opt-out** — it runs on
  every single scan cycle for every config, unconditionally.
- **The backlog cap (30) isn't configurable** — it's a plain module
  constant in `temporal/activities/create_tickets.py`, not exposed via any
  input model or config field.
- **`FERNET_KEY` loss is unrecoverable** — by design, no backdoor exists.
- **`gozu down --wipe` only backs up Postgres** — SonarQube's own volume
  (if `sonarqube-local` is active) is destroyed with no backup at all.
- **`CLAUDE.md` is stale** — still describes `cli/stack.py` as a single
  file, and doesn't mention `cli/wizard_engine.py`, `cli/config_cmd/`,
  `cli/config_lookup.py`, `cli/status.py`, or `core/errors.py`.
- **Zero automated test coverage for everything added in this pass** (§12).
