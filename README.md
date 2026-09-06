# gozu

Scans your code with SonarQube and automatically creates Jira tickets for
vulnerabilities/hotspots it finds - orchestrated with [Temporal](https://temporal.io)
so scans, dedupe, and ticket creation survive crashes/retries.

Working name for the CLI/product; the repo directory is still called
`sonar-to-jira`.

## What happens on a scan

1. Code gets scanned (either you run `gozu run`, or SonarQube fires a webhook after its own analysis).
2. A Temporal workflow fetches open findings from SonarQube.
3. For each finding without an existing ticket (deduped by a Jira label), a ticket is created and dropped into the project's active sprint - up to 30 new tickets per run; anything past that goes into one shared rollup ticket instead (see "Backlog cap" below).
4. A screenshot of the flagged code in the SonarQube UI is captured and attached to the new ticket, along with an extracted code snippet as a comment.
5. Any ticket whose underlying finding SonarQube now reports resolved gets automatically transitioned to done and commented on (see "Auto-closing resolved findings" below) - same run, not a separate step you have to trigger.

Everything runs locally via Docker Compose - Postgres (config storage),
Temporal, and optionally a local SonarQube. Jira is the only piece that has
to be real (Cloud or self-hosted).

## Quickstart

```bash
uv sync                    # install dependencies
uv run gozu init       # interactive wizard: .env, prerequisites, scanner + Jira credentials
gozu up                # bring up Postgres/Temporal (+ SonarQube/receiver if a config needs them)
gozu run                # scan once and create tickets
```

`gozu init` walks you through everything - it writes `.env`, checks for
Java/sonar-scanner (downloading portable copies into `~/.gozu/` if
missing), and saves your scanner + Jira credentials as a named **config**
in Postgres (encrypted at rest). Run it again to add more configs.

## How a scan gets triggered

Three ways, chosen per-config during `gozu init`:

| Mode | When it's used | How it works |
|---|---|---|
| `direct` | Self-hosted SonarQube, run on demand | `gozu run` runs sonar-scanner, waits for SonarQube to finish, then creates tickets |
| `watch` | SonarQube Cloud **Free** plan | Free only analyzes PRs after merge to main - there's no webhook to receive, so `gozu run --watch` polls on an interval instead |
| `webhook` | Self-hosted SonarQube, or SonarQube Cloud **Premium** | SonarQube calls `receiver/app.py` as soon as its own analysis finishes; Premium can track several branches (with pattern support, e.g. `release/*`), each webhook gated against that list |

A Premium config tracking more than one branch fans out: `gozu run`
starts one child workflow per branch under a parent
(`MultiBranchScanWorkflow`), visible in the Temporal UI as a parent with
child workflows. A webhook delivery only ever concerns one branch, so it
never needs to fan out - the branch list just gates which deliveries proceed.

## Auto-closing resolved findings

On by default for every config, no setting to turn it off - every scan
cycle, alongside ticket creation (not a separate trigger you have to run),
gozu checks every ticket it's still tracking as open against SonarQube's
current view of the underlying finding. If SonarQube now reports one of
these resolutions instead of open/reopened:

- **Fixed** - the underlying code issue was actually fixed
- **Won't Fix** - marked as intentionally not going to be fixed
- **False Positive** - marked as not a real issue
- **Removed** - the issue no longer applies (e.g. the file/rule is gone)

...the ticket gets moved to whatever transition in its own Jira workflow
leads to a "done"-category status (gozu never assumes a fixed status name
like "Done"/"Closed" - workflows differ per project), with a comment
explaining why (e.g. "Closed automatically - SonarQube marked this False
Positive"). If a ticket's current workflow has no transition into a
"done" status available at all, gozu logs that and leaves it alone rather
than guessing at the wrong transition.

## Backlog cap

Each run creates at most **30** new tickets for a given project (a scan
against a brand-new/large codebase can otherwise return hundreds of
findings, and Jira issue-creation isn't free). Anything past the first 30
new findings doesn't get skipped - it's rolled into one shared "backlog"
ticket (tagged `gozu-backlog-rollup`, distinct from the per-finding
`source-key-{key}` labels) listing those findings' keys/rules/severities.

That rollup ticket updates in place on every later run instead of a new
one appearing each time: `--watch`/webhook mode re-runs against what's
often the same persistent backlog, so a fresh scan finding the exact same
80 leftover findings doesn't create an 81st "80 more findings" ticket - it
edits the existing one. As more of the backlog gets ticketed for real in
later runs (30 more each time), the rollup ticket's count goes back down,
reaching 0 once the backlog's fully worked through.

## Project layout

```
cli/                    the `gozu` CLI (typer)
  main.py                 entrypoint: init / run / up / down
  init_wizard/             `gozu init` wizard, one file per step
  scan_runner/             `gozu run`: scan, poll, trigger workflow
  prerequisites/           downloading/verifying Java + sonar-scanner
  help_links.py            credential help-link lookup
  stack/                   `gozu up`/`down` (+ `--wipe`), Compose profile detection

config/                 saved scanner+ticket credential sets ("configs")
  store.py                 CRUD against Postgres
  connection.py            shared get_connection() (store.py + ticket_destinations.py)
  ticket_destinations.py   shared ticket boards multiple configs can reference
  crypto.py                Fernet encryption for stored secrets

scanner/                scanner backends
  base.py                  ScannerClient interface + shared constants
  sonarqube_server.py       self-hosted SonarQube
  sonarqube_cloud.py        SonarQube Cloud (stub)
  factory.py                build_scanner_client() / get_scanner_client()
  screenshot.py            Playwright: screenshot + code extraction

ticket/                 ticket backends
  base.py                  TicketClient interface
  jira_client.py            Jira implementation (incl. transitions, rollup ticket)
  jira_sprint.py            active-sprint lookup/assignment
  claims.py                idempotency ledger + open/closed status (Postgres)
  adf.py                   Atlassian Document Format builders
  factory.py                build_ticket_client() / get_ticket_client()

temporal/               Temporal workflows/activities/models
  worker.py                worker process entrypoint
  workflows/               ScanToTicketWorkflow, MultiBranchScanWorkflow
  activities/               fetch findings, create tickets, reconcile resolved findings, screenshot
  models/                  per-activity Pydantic input models

receiver/               Flask webhook receiver
  app.py                   routes, signature + branch gating
  verify_signature.py      HMAC verification
  starter.py               starts a workflow on behalf of a webhook

core/models.py          shared domain models (Finding, TicketResult, ...)
scripts/                 one-off/bootstrap scripts (env setup, seeding)
sql/init.sql             Postgres schema
```

## Adding a new scanner or ticket backend

Both scanner/ and ticket/ follow the same shape: an abstract base class
(`scanner/base.py`'s `ScannerClient`, `ticket/base.py`'s `TicketClient`),
one implementation module per backend, and a `factory.py` with a pure
`build_*_client()` (explicit params, no env reads) plus a thin
`get_*_client()` env-reading wrapper used by the webhook receiver.

To add a backend:
1. Write a new class implementing the interface (see `scanner/sonarqube_server.py` or `ticket/jira_client.py` for the shape).
2. Register it in that package's `factory.py`.
3. If it's a scanner, add it to `scanner/base.py`'s `SCANNER_REGISTRY` so the init wizard can offer it.

Nothing else in the codebase needs to change - workflows, activities, and
the receiver only ever talk to the abstract interface.

## Configuration model

A **config** (never called a "profile" - Docker Compose already has an
unrelated `profiles` concept) is a named set of scanner + ticket
credentials, stored in Postgres (`sql/init.sql`) via `config/store.py`.
Every secret value is Fernet-encrypted (`config/crypto.py`) before it
touches the database; `FERNET_KEY` lives only in `.env`, never committed.

`gozu init` creates configs; `gozu run --config <name>` uses one;
`gozu up` inspects all of them to decide which Docker Compose profiles
(`sonarqube-local`, `webhook`) need to be running.

### Ticket destinations - one shared board, many configs

A config's Jira credentials can live in one of two places, and `gozu`
doesn't care which when it actually creates tickets:

- **Embedded** (every config created before this feature existed): the
  config's own `config_credentials` rows carry its Jira URL/email/API
  token/project key directly - the original shape, still fully supported.
- **A shared ticket destination** (`config/ticket_destinations.py`, new
  configs by default): the config just stores a `ticket_destination_id`
  pointing at one `ticket_destinations` row, and any number of other
  configs can point at that same row instead of each embedding their own
  copy of the same credentials.

Run `gozu init` a second time (a second repo, a second scanner config,
whatever) and pick **Local or Cloud** as usual - when it gets to the Jira
step, if a destination already exists you'll see "Use an existing ticket
destination, or create a new one?" instead of being asked for a Jira
URL/email/token again. Pick the existing one and the wizard skips straight
to naming the config - zero Jira prompts. Both configs' tickets land on
the same board, and dedupe (`ticket/claims.py`) still holds correctly
across them, since it keys off the destination itself, not which config
triggered the scan.

## Development

```bash
uv run pytest          # tests
uv run ruff check .    # lint
uv run --with pyright pyright .   # types
```

Conventions (see `CLAUDE.md` for the full list): Pydantic `BaseModel` for
every data model, `workflow.logger`/`activity.logger` inside Temporal code
(plain `logging` only in `receiver/app.py` and `temporal/worker.py`), and
questionary for every interactive prompt in the wizard.

## Docker Compose profiles

Always-on: `postgres`, `temporal`. Conditionally started by `gozu up`
based on your configs:

- `sonarqube-local` - a local SonarQube instance, for configs using self-hosted scanning
- `webhook` - the Flask receiver, for any config in `webhook` trigger mode

`gozu down` and `gozu up` determine which of `sonarqube-local`/`webhook`
are active the exact same way (`cli/stack/profiles.py`'s
`active_profiles()`), so `down` correctly stops whichever of those got
started - not just the always-on services.

### `gozu down --wipe`

`gozu down` alone only stops containers - volumes/data persist, and
nothing is destroyed. `gozu down --wipe` is the one irreversible command
in this CLI: it also deletes Postgres's volume (every config, ticket
destination, and dedupe claim) and, if `sonarqube-local` was an active
profile this run, local SonarQube's volume (scan history) too. Before
doing anything destructive it prints exactly what it's about to delete -
real row counts, not an estimate - along with the path a Postgres backup
will be written to, and requires typing the literal word `wipe` to
proceed; anything else cancels with zero side effects (the stack still
ends up stopped from the non-destructive part, just not wiped, and no
backup is created for a cancelled wipe).

Once confirmed, a `pg_dump` of Postgres (configs, ticket destinations,
dedupe claims - not SonarQube's volume, that's deliberately out of scope)
is written to `~/.gozu/backups/wipe-<timestamp>.sql` before anything is
actually deleted - the printed path is real, not aspirational. Backups
older than 7 days are pruned the next time a wipe runs
(`cli/stack/backup.py`'s `_RETENTION_DAYS`) - there's no separate
scheduled cleanup job.
