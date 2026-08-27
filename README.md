# sonar-to-jira (local, zero-cost POC)

Proof of concept for a pipeline that will eventually auto-create Jira
tickets from SonarQube vulnerabilities/hotspots, orchestrated with
Temporal. Everything below runs locally and free.

**Current scope:** webhook fires -> signature verified -> Temporal workflow
starts -> an activity fetches real vulnerabilities/hotspots from local
SonarQube -> a second activity creates a Jira ticket for each issue that
doesn't already have one (dedupe via a label), skipping the rest, and
drops new tickets straight into the project's active sprint if one exists.

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

Switching from self-hosted SonarQube to SonarQube Cloud later should mean:

1. Implementing `fetch_findings()` on `scanner/client.py`'s `SonarQubeCloudClient`.
2. Setting `SCANNER_TYPE=sonarqube-cloud` in `.env`.

**Serialization note:** `Finding` and `Severity` are a plain dataclass/Enum,
not Pydantic models, so they don't cross the Temporal workflow/activity
boundary automatically the way `TicketResult`/`CreatedTicket`/
`SonarToJiraInput` (all Pydantic `BaseModel`s, handled by the Pydantic-aware
data converter in `temporal/data_converter.py`) do.
`fetch_findings_activity` converts each `Finding` to a `dict` with
`dataclasses.asdict()` (plus `severity.value` for the enum) before
returning it, and `create_tickets_activity` reconstructs `Finding` objects
from those dicts on the way in.

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
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

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
   confirm it **Completed**. Open it and check the result of the second
   activity (`create_tickets_activity`) - it should show a
   `created` list with one entry per flagged finding and an empty `skipped`
   list on a first run.
2. **worker terminal** (`python -m temporal.worker`) - look for lines like:
   ```
   Tickets: created 1 ticket(s), skipped 0 already-ticketed finding(s)
     created SONAR-1 for finding AbCdEfGh...
   ```
3. **Jira** - open your project in Jira Cloud, confirm new ticket(s)
   appeared with a descriptive summary, type `Bug`, and
   labels including `sonarqube`, `security`, and a
   `source-key-<finding-key>` label. If your project has an **active
   sprint**, the ticket lands directly on the **Board** tab; if not, it's
   in the **Backlog** tab instead (new tickets never appear on the board
   without an active sprint to put them in).

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
    fetch_findings.py          fetch_findings_activity (calls the ScannerClient interface)
    create_tickets.py          create_tickets_activity (dedupe + create via the TicketClient interface)
  workflows/
    scan_to_ticket.py          ScanToTicketWorkflow
  models/
    sonar_to_jira.py           SonarToJiraInput (the workflow's input model)

core/
  models.py                    Finding, Severity (normalized vocabulary), CreatedTicket, TicketResult

scanner/
  client.py                    ScannerClient (ABC) - the scanner extension point.
                                SonarQubeServerClient (real) + SonarQubeCloudClient (stub) +
                                get_scanner_client() (picks one based on SCANNER_TYPE)

ticket/
  client.py                    TicketClient (ABC) - the ticket extension point.
                                JiraClient + get_ticket_client() (picks one based on TICKET_BACKEND)

requirements.txt
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
