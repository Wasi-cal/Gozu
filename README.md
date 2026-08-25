# sonar-to-jira (local, zero-cost POC)

Proof of concept for a pipeline that will eventually auto-create Jira
tickets from SonarQube vulnerabilities/hotspots, orchestrated with
Temporal. Everything below runs locally and free.

**Current scope:** webhook fires -> signature verified -> Temporal workflow
starts -> an activity fetches real vulnerabilities/hotspots from local
SonarQube -> a second activity creates a Jira ticket for each issue that
doesn't already have one (dedupe via a label), skipping the rest.

## Architecture

```
SonarQube (Docker)
   |  webhook (HMAC-SHA256 signed)
   v
receiver.py (Flask, :5001)
   |  verifies signature, starts workflow
   v
Temporal server (:7233)
   |
   v
worker.py -> workflows.py (SonarToJiraWorkflow)
                  |
                  v
             activities.py (fetch_vulnerabilities_activity)
                  |
                  v
             sonar_client.py (SonarClient interface)
                  |
        -------------------
        |                 |
 SonarQubeServerClient   SonarQubeCloudClient (stub, NotImplementedError)
   (self-hosted, real)
                  |
                  v
             activities.py (create_jira_tickets_activity)
                  |
                  v
             jira_client.py (JiraClient)
                  |
                  v
             Jira Cloud (find_existing_ticket dedupe, then create_ticket)
```

The `SonarClient` abstract interface in `sonar_client.py` is the seam
between this project and whichever Sonar product is in use. Switching
from self-hosted SonarQube to SonarQube Cloud later should mean:

1. Implementing the two methods on `SonarQubeCloudClient`.
2. Setting `SONAR_MODE=cloud` in `.env`.

Nothing in `workflows.py`, `activities.py`, or `receiver.py` should need
to change.

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

In SonarQube: **My Account > Security > Generate Tokens**. Copy the token,
you'll put it in `.env` as `SONAR_TOKEN`.

## 3. Scan the sample project

Install the [SonarScanner CLI](https://docs.sonarsource.com/sonarqube/latest/analyzing-source-code/scanners/sonarscanner/)
locally, then from the `sample_project/` directory:

```bash
cd sample_project
sonar-scanner \
  -Dsonar.login=<your-token>
```

This uses `sonar-project.properties` already in that folder. After it
finishes, refresh the SonarQube UI - you should see the project
`sonar-to-jira-sample` with one flagged vulnerability (the hardcoded API
key in `app.py`).

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

**Why dedupe uses a label instead of a custom field:** a label is a
built-in field every Jira project already has - no setup, no
permissions, works immediately on a brand-new free-tier project. A
custom field would need to be created and explicitly added to the
project's screen/permission scheme before the API could read or write it,
which is extra one-time admin work per project. For a personal POC, a
label (`sonar-key-{sonar_issue_key}`) gets the same dedupe behavior for
zero setup cost.

## 6. Set up the Python environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and fill in:

- `SONAR_WEBHOOK_SECRET` - matches the secret you set on the webhook
- `SONAR_TOKEN` - the token from step 2
- Leave `SONAR_MODE=local` and `SONAR_HOST_URL=http://localhost:9000` as-is
- `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY` - from step 5

Because `.env` isn't loaded automatically by these scripts, export it into
your shell before running anything:

```bash
export $(grep -v '^#' .env | xargs)
```

## 7. Start the Temporal dev server

In its own terminal:

```bash
temporal server start-dev
```

This starts Temporal on `localhost:7233` and a Web UI at
http://localhost:8233 where you can watch workflow executions.

## 8. Start the Temporal worker

In another terminal (with the venv activated and `.env` exported):

```bash
python worker.py
```

## 9. Start the webhook receiver

In another terminal (same venv/env):

```bash
python receiver.py
```

This starts Flask on `http://localhost:5001`.

## 10. Trigger it

Re-run the scanner against the sample project (or click **"Trigger
webhook"** manually if SonarQube's admin UI exposes it, depending on
version):

```bash
cd sample_project
sonar-scanner -Dsonar.login=<your-token>
```

When the analysis finishes, SonarQube fires the webhook -> `receiver.py`
verifies the signature -> starts `SonarToJiraWorkflow` -> the workflow
calls `fetch_vulnerabilities_activity` (hits SonarQube via
`SonarQubeServerClient`) -> then `create_jira_tickets_activity` (hits
Jira via `JiraClient`, checking `find_existing_ticket` before creating
anything) -> results are logged.

### How to confirm success

1. **Temporal Web UI** (http://localhost:8233) - find the workflow run and
   confirm it **Completed**. Open it and check the result of the second
   activity (`create_jira_tickets_activity`) - it should show a
   `created` list with one entry (the hardcoded-secret issue) and an
   empty `skipped` list on a first run.
2. **`worker.py` terminal** - look for lines like:
   ```
   Jira: created 1 ticket(s), skipped 0 already-ticketed issue(s)
     created SONAR-1 for sonar issue AbCdEfGh...
   ```
3. **Jira project board** - open your project in Jira Cloud, confirm a new
   ticket appeared with summary `[Sonar] ... in app.py:...`, type `Bug`,
   and labels including `sonarqube`, `security`, and a
   `sonar-key-<sonar-issue-key>` label.

### Verifying dedupe (no duplicate tickets on a re-scan)

1. With a ticket already created from step 10 above, re-run the exact same
   scan without changing any code:
   ```bash
   cd sample_project
   sonar-scanner -Dsonar.login=<your-token>
   ```
   SonarQube re-analyzes the file, finds the same issue (same stable
   issue key, since nothing changed), and fires the webhook again with a
   new `taskId`.
2. Watch the `worker.py` terminal for the second run. You should now see:
   ```
   Jira: created 0 ticket(s), skipped 1 already-ticketed issue(s)
     skipped sonar issue AbCdEfGh... (ticket already exists)
   ```
   The `skipped` count should match the number of issues that had tickets
   from the previous run, and `created` should be empty (assuming no new
   issues were introduced).
3. Cross-check in Temporal Web UI: open the second workflow execution's
   result and confirm `created` is `[]` and `skipped` contains the same
   Sonar issue key from the first run.
4. Cross-check in Jira: refresh the project board and confirm there is
   still exactly **one** ticket with the `sonar-key-<key>` label - not
   two.

If you want to force a *new* ticket to prove dedupe isn't just "always
skip," add a second hardcoded secret to `sample_project/app.py`, re-scan,
and confirm exactly one new ticket is created (`created` has 1 entry) while
the original issue is still skipped.

## Testing the receiver without a real SonarQube webhook

You can hand-craft a signed request to test the receiver in isolation:

```python
import hmac, hashlib, json, requests

secret = "changeme"  # must match SONAR_WEBHOOK_SECRET
body = json.dumps({
    "taskId": "test-task-1",
    "project": {"key": "sonar-to-jira-sample"},
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

## Project structure

```
receiver.py                 Flask app, POST /webhooks/sonarqube
sonar_client.py              SonarClient interface + SonarQubeServerClient (real) + SonarQubeCloudClient (stub)
jira_client.py                JiraClient: find_existing_ticket (dedupe) + create_ticket
activities.py                Temporal activities: fetch_vulnerabilities_activity, create_jira_tickets_activity
workflows.py                 Temporal workflow: SonarToJiraWorkflow
worker.py                    Temporal worker (task queue: sonar-jira-queue)
requirements.txt
.env.example
sample_project/               Tiny scannable project with one obvious flagged issue
  app.py
  sonar-project.properties
```

## What's not built yet

- `SonarQubeCloudClient` real implementation
- Updating/transitioning existing Jira tickets when a Sonar issue is
  resolved (currently only create + dedupe-skip; no closing logic)
