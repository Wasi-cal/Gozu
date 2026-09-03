# codescan

Scans your code with SonarQube and automatically creates Jira tickets for
vulnerabilities/hotspots it finds - orchestrated with [Temporal](https://temporal.io)
so scans, dedupe, and ticket creation survive crashes/retries.

Working name for the CLI/product; the repo directory is still called
`sonar-to-jira`.

## What happens on a scan

1. Code gets scanned (either you run `codescan run`, or SonarQube fires a webhook after its own analysis).
2. A Temporal workflow fetches open findings from SonarQube.
3. For each finding without an existing ticket (deduped by a Jira label), a ticket is created and dropped into the project's active sprint.
4. A screenshot of the flagged code in the SonarQube UI is captured and attached to the new ticket, along with an extracted code snippet as a comment.

Everything runs locally via Docker Compose - Postgres (config storage),
Temporal, and optionally a local SonarQube. Jira is the only piece that has
to be real (Cloud or self-hosted).

## Quickstart

```bash
uv sync                    # install dependencies
uv run codescan init       # interactive wizard: .env, prerequisites, scanner + Jira credentials
codescan up                # bring up Postgres/Temporal (+ SonarQube/receiver if a config needs them)
codescan run                # scan once and create tickets
```

`codescan init` walks you through everything - it writes `.env`, checks for
Java/sonar-scanner (downloading portable copies into `~/.codescan/` if
missing), and saves your scanner + Jira credentials as a named **config**
in Postgres (encrypted at rest). Run it again to add more configs.

## How a scan gets triggered

Three ways, chosen per-config during `codescan init`:

| Mode | When it's used | How it works |
|---|---|---|
| `direct` | Self-hosted SonarQube, run on demand | `codescan run` runs sonar-scanner, waits for SonarQube to finish, then creates tickets |
| `watch` | SonarQube Cloud **Free** plan | Free only analyzes PRs after merge to main - there's no webhook to receive, so `codescan run --watch` polls on an interval instead |
| `webhook` | Self-hosted SonarQube, or SonarQube Cloud **Premium** | SonarQube calls `receiver/app.py` as soon as its own analysis finishes; Premium can track several branches (with pattern support, e.g. `release/*`), each webhook gated against that list |

A Premium config tracking more than one branch fans out: `codescan run`
starts one child workflow per branch under a parent
(`MultiBranchScanWorkflow`), visible in the Temporal UI as a parent with
child workflows. A webhook delivery only ever concerns one branch, so it
never needs to fan out - the branch list just gates which deliveries proceed.

## Project layout

```
cli/                    the `codescan` CLI (typer)
  main.py                 entrypoint: init / run / up / down
  init_wizard/             `codescan init` wizard, one file per step
  scan_runner/             `codescan run`: scan, poll, trigger workflow
  prerequisites/           downloading/verifying Java + sonar-scanner
  help_links.py            credential help-link lookup
  stack.py                 `codescan up`/`down` (docker compose)

config/                 saved scanner+ticket credential sets ("configs")
  store.py                 CRUD against Postgres
  crypto.py                Fernet encryption for stored secrets

scanner/                scanner backends
  base.py                  ScannerClient interface + shared constants
  sonarqube_server.py       self-hosted SonarQube
  sonarqube_cloud.py        SonarQube Cloud (stub)
  factory.py                build_scanner_client() / get_scanner_client()
  screenshot.py            Playwright: screenshot + code extraction

ticket/                 ticket backends
  base.py                  TicketClient interface
  jira_client.py            Jira implementation
  jira_sprint.py            active-sprint lookup/assignment
  adf.py                   Atlassian Document Format builders
  factory.py                build_ticket_client() / get_ticket_client()

temporal/               Temporal workflows/activities/models
  worker.py                worker process entrypoint
  workflows/               ScanToTicketWorkflow, MultiBranchScanWorkflow
  activities/               fetch findings, create tickets, screenshot
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

`codescan init` creates configs; `codescan run --config <name>` uses one;
`codescan up` inspects all of them to decide which Docker Compose profiles
(`sonarqube-local`, `webhook`) need to be running.

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

Always-on: `postgres`, `temporal`. Conditionally started by `codescan up`
based on your configs:

- `sonarqube-local` - a local SonarQube instance, for configs using self-hosted scanning
- `webhook` - the Flask receiver, for any config in `webhook` trigger mode

`codescan down` stops containers; volumes/data persist. There's no
destructive wipe command - use `docker compose down -v` yourself if you
actually want to drop data.
