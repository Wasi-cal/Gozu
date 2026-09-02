# How this project works

This document explains the internals of the Sonar-to-Jira pipeline: what
happens end-to-end when a scan finishes, why each piece is built the way
it is, and the non-obvious decisions (and bugs found along the way) baked
into the current code. For setup/installation steps, see `README.md` —
this document assumes the system is already running and focuses on how
it behaves.

## One-paragraph summary

SonarQube analyzes a project and fires a webhook. A Flask receiver
verifies the webhook's signature and starts a Temporal workflow. That
workflow fetches the project's open vulnerabilities and security
hotspots from SonarQube (via the generic `ScannerClient` interface),
creates a Jira ticket for each one that doesn't already have one
(deduping via a label, via the generic `TicketClient` interface), and
then — for every ticket it just created — concurrently captures a
screenshot of the flagged code in SonarQube's UI, extracts the code and
SonarQube's inline annotation as text, attaches the screenshot to the
ticket, and posts the extracted text as a comment. A broken screenshot
can never affect ticket creation or dedupe; that isolation is the
central design constraint of the whole system.

## Architecture

```
SonarQube (Docker, self-hosted Community Build)
   |  webhook fires when analysis finishes (HMAC-SHA256 signed)
   v
receiver/app.py (Flask, :5001)
   |  verify_signature.py checks the HMAC signature
   |  starter.py connects to Temporal and starts ScanToTicketWorkflow
   v
Temporal server (:7233)
   v
temporal/worker.py -> temporal/workflows/scan_to_ticket.py (ScanToTicketWorkflow)
   |
   |--1--> temporal/activities/fetch_findings.py
   |         -> scanner/client.py's get_scanner_client() picks a ScannerClient
   |         -> SonarQubeServerClient hits SonarQube's REST API
   |         -> stamps finding.branch from local git state
   |
   |--2--> temporal/activities/create_tickets.py
   |         -> ticket/client.py: find_existing (dedupe) -> create_ticket
   |         -> new tickets get moved into the active sprint, if one exists
   |
   |--3--> (fanned out concurrently, once per ticket just created)
             temporal/activities/capture_and_attach_screenshot.py
               -> scanner/screenshot.py: Playwright screenshots the finding's
                  source-viewer panel AND extracts code + annotation text
               -> ticket/client.py: attach_screenshot (idempotent) + add_comment
```

Activities 1 and 2 run sequentially with the SDK's default retry policy
and a 30s timeout each. Activity 3 runs with its own 45s timeout and
`RetryPolicy(maximum_attempts=2)` — deliberately more lenient, because
browser automation is slower and flakier than a REST call, and a missing
screenshot is not worth retrying aggressively.

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
   (used to build a deterministic workflow ID:
   `sonar-to-jira-{project_key}-{task_id}`, so re-delivering the same
   webhook doesn't start a duplicate workflow run).
4. The workflow's first activity, `fetch_findings_activity`, asks
   `scanner/client.py`'s `get_scanner_client()` for a `ScannerClient`
   (currently always `SonarQubeServerClient`, since `SCANNER_TYPE=sonarqube`),
   whose `fetch_findings()` hits `/api/issues/search` (type=VULNERABILITY)
   and `/api/hotspots/search` (status=TO_REVIEW) and turns the raw JSON
   into normalized `Finding` objects. Before returning, the activity
   stamps every finding's `branch` field by shelling out to
   `git rev-parse --abbrev-ref HEAD` in the worker's own working
   directory (see [Why branch is read from local git](#why-branch-is-read-from-local-git) below).
5. The second activity, `create_tickets_activity`, loops over every
   finding. For each one it calls `find_existing`, which searches Jira
   via JQL for a ticket labeled `source-key-{finding.key}`. If found, the
   finding is recorded as skipped. If not, `create_ticket` POSTs a new
   Jira issue (type `Bug`, priority mapped from the normalized severity,
   labeled `sonarqube`, `security`, and `source-key-{finding.key}`) and,
   if the project has an active sprint, moves it there.
6. If any tickets were created, the workflow fans out
   `capture_and_attach_screenshot_activity` once per created ticket,
   concurrently, via `asyncio.gather(..., return_exceptions=True)`. Each
   invocation:
   - Opens the finding's SonarQube deep link in a headless, authenticated
     Chromium session (`scanner/screenshot.py`).
   - Screenshots the matched source-viewer element to a temp PNG.
   - Extracts the flagged code's text and SonarQube's inline annotation
     text from the same page load (no second browser trip), returning
     both plus the screenshot path as a `FindingExtraction`.
   - Attaches the PNG to the Jira ticket (`attach_screenshot`, skipping
     if a same-named file is already there).
   - If any text was extracted, posts it as a Jira comment
     (`add_comment`), formatted as a single code block.
   - Cleans up its temp directory in a `finally`, whether it succeeded or
     not.
7. `asyncio.gather(..., return_exceptions=True)` means a permanently
   failing screenshot (after its own 2 retry attempts) shows up as an
   exception object in the results list rather than raising — the
   workflow logs a warning per failure and still returns the
   `TicketResult` from step 5 successfully. **The screenshot/comment
   step can never turn a successful ticket-creation run into a failed
   workflow.**

## Component reference

### `receiver/` — the webhook entrypoint

- **`app.py`** — the only Flask route is `POST /webhooks/sonarqube`. It
  verifies the signature, extracts `project.key` from the JSON body
  (400 if missing), starts the workflow, and returns 200 immediately —
  all the real work happens asynchronously in Temporal, so the webhook
  response doesn't wait on SonarQube API calls, Jira API calls, or
  browser automation.
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
  `FindingExtraction`, ...), so this converter serializes every one of
  them across the workflow/activity boundary automatically — no manual
  `.model_dump()`/`.model_validate()`, and no dataclass-to-dict
  workarounds, anywhere.
- **`worker.py`** — connects to Temporal, registers the one workflow and
  three activities as plain imported function objects (not by string
  name), and blocks on `worker.run()`. Any new activity must be imported
  and added to the `activities=[...]` list here or it silently can't be
  scheduled (the workflow would hang waiting for a worker that never
  claims the task).
- **`workflows/scan_to_ticket.py`** — the only orchestration logic in the
  system. Notice what it does *not* do: no try/except around
  `execute_activity` calls for the first two activities. Temporal's own
  retry policy (default, since none is passed) handles transient
  failures there, and if `fetch_findings_activity` or
  `create_tickets_activity` fails permanently, the whole workflow is
  meant to fail loudly — there's no reason to hide a broken
  SonarQube/Jira connection. The screenshot activity is the only one
  wrapped in failure-tolerant handling, via `return_exceptions=True`.
- **`models/`** — one file per activity-boundary model
  (`sonar_to_jira.py` → `SonarToJiraInput`, `screenshot_attach.py` →
  `ScreenshotAttachInput`), matching this repo's one-small-model-per-file
  convention.
- **`activities/`** — see the walkthrough above; each activity is a
  single `@activity.defn async def` function with no shared state
  between them beyond what's passed as arguments.

### `core/models.py` and the scanner/ticket seam

Unlike the pre-refactor version of this codebase (a direct SonarQube ->
Jira pipeline), the current system is generic over which scanner and
which ticketing system sit behind it:

- **`core/models.py`** — `Finding` and `Severity` are the normalized
  vocabulary every adapter translates into/out of. Neither
  `scanner/client.py` nor `ticket/client.py` ever sees the other's
  tool-specific values (SonarQube's `BLOCKER`/`MAJOR`, Jira's issue
  keys) outside its own adapter. See [Data model reference](#data-model-reference)
  for every field.
- **`scanner/client.py`** — `ScannerClient` is an `ABC` with one method,
  `fetch_findings(project_key) -> list[Finding]` (combining vulnerabilities
  and hotspots into one list is an internal detail of each adapter, not
  part of the generic contract). `get_scanner_client()` is the single
  place that reads `SCANNER_TYPE` and picks a concrete class — the
  comment in the code calls this out explicitly: *"the ONLY place in the
  codebase that should know which concrete class is in use."*
  - `SonarQubeServerClient` — the real implementation, talking to
    self-hosted SonarQube. Auth is HTTP Basic with the token as username
    and an empty password (`(token, "")`), which is SonarQube's
    documented convention for using a personal access token in place of
    a username/password pair on its REST API.
  - `SonarQubeCloudClient` — a deliberate stub. `fetch_findings` raises
    `NotImplementedError`; switching to it later should mean "flip
    `SCANNER_TYPE=sonarqube-cloud` and implement this one method,
    nothing else in the codebase needs to change." Nothing currently
    exercises this path.
- **`scanner/screenshot.py`** — see the deep dive below; this file has
  the most interesting failure history in the codebase. Not part of the
  `ScannerClient` contract — it's a SonarQube-specific bonus capability
  (Sonar's deep-link URL shape, `SONAR_TOKEN` auth), called directly by
  the screenshot activity rather than through `get_scanner_client()`.

#### The screenshot/extraction story

`capture_finding_screenshot(finding, out_path) -> FindingExtraction` is
the one function this module exports. Getting it right took three
separate bugs found through live testing, each worth understanding
because the fixes are non-obvious:

**1. Playwright's async API is required, not a style choice.** Temporal
runs `async def` activities as coroutines directly on the worker's own
event loop. Playwright's *sync* API detects a running event loop in the
calling thread and refuses to start. This is the one place in the
codebase that doesn't follow the "call blocking I/O directly from
`async def` activities" pattern used everywhere else (`requests` calls
in `ticket/client.py` and `scanner/client.py` are synchronous and simply
block the loop, which is fine for a local POC's request volume;
Playwright's sync API can't even be *called* from a running loop, so
this isn't optional).

**2. `http_credentials` silently never authenticates.** An earlier
version of this module used Playwright's `new_context(http_credentials=...)`
option — the "correct-looking" way to do HTTP Basic auth in a browser
context. It doesn't work here: `http_credentials` only attaches the
`Authorization` header *after* the server responds `401 Unauthorized` to
challenge it. SonarQube's web app always returns `200` for its SPA shell
regardless of auth state (the React app then redirects client-side if
you're not logged in) — so the challenge that `http_credentials` waits
for never happens, and the header is never sent. The screenshot
"succeeded" every time; it just silently screenshotted SonarQube's login
page and attached that to real tickets. The fix (`_get_context` in the
current code) sets the header unconditionally via
`extra_http_headers={"Authorization": f"Basic {token}"}` instead, which
Playwright sends on every request regardless of any challenge.

**3. A short browser viewport clips the source table before it renders.**
Even after fixing auth, screenshots sometimes came back showing just the
inline issue-annotation tooltip with the actual flagged code line
scrolled out of frame — because SonarQube's source-code table doesn't
fully render/expand at a normal viewport height until scrolled. The fix
is a generously tall viewport (`1280x2000`) set once on the shared
browser context, so the whole snippet renders without needing to scroll
at all.

With those three fixed, `capture_finding_screenshot` does, in order:

1. Get (or lazily create) a single shared `Browser`/`BrowserContext` for
   the life of the worker process — one browser launch total, not one
   per finding.
2. Open a new `Page`, navigate to
   `finding.deep_link + f"&open={finding.key}"` (SonarQube's deep-link
   format already contains the project key and issue key; appending
   `&open=` is what makes the source viewer auto-expand to that specific
   issue).
3. Try a short list of CSS selectors in order (`table`,
   `[data-testid="source-viewer"]`, `.source-viewer`) until one becomes
   visible, screenshot that element, then:
   - Call `.inner_text()` on that same matched locator to get
     `code_snippet` — no second page load, same Playwright session.
   - Call `_extract_annotation_text(page, finding)` to get
     `annotation_text`. **This one is scoped to the whole `page`, not
     the matched locator** — live DOM inspection found that SonarQube's
     inline annotation callout is *not* a descendant of the source-code
     table at all (initial attempts to scope this search inside the
     table matched either nothing, or the wrong thing — the underlined
     code token itself, via an over-broad `[class*="issue"]` selector,
     producing garbage like `"Flask"` instead of the real message). The
     real annotation lives in a separate `<header>` element elsewhere on
     the page; when multiple `<header>`s exist (the top nav bar is also
     one), the *last* one is the issue-detail header, and its first line
     of text is exactly SonarQube's issue message.
4. If no selector matches at all, fall back to a full-page screenshot
   with no text extraction (there's no reliable region to scope
   extraction to at that point) rather than hard-failing the whole
   activity.
5. Any exception during text extraction specifically (not screenshot
   capture) is caught, logged as a warning via `activity.logger`, and the
   corresponding field is left `None` — a broken extraction never
   prevents the screenshot itself from succeeding and being returned.

This selector-guessing approach is inherently coupled to SonarQube's
current UI markup (React app with CSS-in-JS hashed class names — there's
no stable `data-testid` for either the source viewer or the annotation
box in this version). If a SonarQube upgrade changes the DOM, the
`_SOURCE_VIEWER_SELECTORS` / header-based annotation lookup may need
re-verifying against a live instance the same way they were discovered.

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

### `ticket/client.py` — the Jira Cloud integration

- **`TicketClient`** is an `ABC` with two methods
  (`find_existing`, `create_ticket`) — the seam between "some ticketing
  product" and everything upstream; nothing outside `ticket/client.py`
  should ever construct `JiraClient` directly. `get_ticket_client()` is
  the single place that reads `TICKET_BACKEND` and picks a concrete
  class.
- **`JiraClient`** — the only concrete implementation today. Every
  method follows the same shape: build a `requests` call with
  `auth=self.auth`, check the response via `_raise_for_status` (a plain
  `RuntimeError` with the status code and body — no custom exception
  hierarchy anywhere in this codebase).
  - `find_existing` / `create_ticket` — the dedupe mechanism (the
    `TicketClient` ABC's two required methods). Dedupe is a Jira
    **label** (`source-key-{finding.key}`), not a custom field,
    specifically so it works on a brand-new free-tier project with zero
    admin setup (custom fields need to be added to a project's
    screen/permission scheme before the API can use them; labels are a
    built-in field on every project).
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
| `FindingExtraction` | `scanner/screenshot.py` | `screenshot_path`, `code_snippet`, `annotation_text` | No — produced and consumed entirely inside `capture_and_attach_screenshot_activity`'s function body; never serialized by Temporal. Still a Pydantic `BaseModel`, for consistency with the rest of the codebase, not because Temporal requires it here. |

## Testing

The `tests/` tree mirrors the source layout
(`tests/scanner/test_screenshot.py`, `tests/temporal/activities/test_capture_and_attach_screenshot.py`)
and needs no live SonarQube, Jira, or browser — everything is mocked
with stdlib `unittest.mock`:

- **`tests/scanner/test_screenshot.py`** fakes Playwright's `page`/
  `locator` objects to verify: `inner_text()` lands in `code_snippet`;
  a text-extraction failure still returns a usable `FindingExtraction`
  (never raises); the page-level `<header>` lookup correctly finds
  `annotation_text` when headers exist and returns `None` when they
  don't or when the lookup itself raises.
- **`tests/temporal/activities/test_capture_and_attach_screenshot.py`**
  fakes the ticket client (via `get_ticket_client`) to verify:
  `add_comment` is called with a body containing both extracted fields
  when extraction succeeds; both bonus methods are skipped cleanly (with
  a logged warning, no exception) when a client double doesn't have
  them; a **real** error from a method that *does* exist still
  propagates out of the activity (pinning down the "getattr-is-None
  only" soft-fail boundary described above); the temp directory is
  removed even when an exception propagates.

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
| `SCANNER_TYPE` | `scanner/client.py` | `sonarqube` or `sonarqube-cloud` — picks the `ScannerClient` implementation (falls back to the legacy `SONAR_MODE` var if unset) |
| `SONAR_HOST_URL` | `scanner/client.py`, `scanner/screenshot.py` (via `finding.deep_link`) | Base URL of the SonarQube instance |
| `SONAR_TOKEN` | `scanner/client.py`, `scanner/screenshot.py` | User token; used both as the REST API's Basic-auth username and as the Playwright browser session's Basic-auth token |
| `SONAR_ORGANIZATION` | `scanner/client.py` | Only used when `SCANNER_TYPE=sonarqube-cloud` (currently unimplemented) |
| `TICKET_BACKEND` | `ticket/client.py` | `jira` — picks the `TicketClient` implementation |
| `JIRA_URL` | `ticket/client.py` (via activities) | Base URL of the Jira Cloud site |
| `JIRA_EMAIL` | `ticket/client.py` (via activities) | Account the API token belongs to |
| `JIRA_API_TOKEN` | `ticket/client.py` (via activities) | Jira Cloud API token |
| `JIRA_PROJECT_KEY` | `ticket/client.py` (via activities) | Which Jira project tickets are created in |

## Known limitations

- **`SonarQubeCloudClient` is an unimplemented stub.** Setting
  `SCANNER_TYPE=sonarqube-cloud` will raise `NotImplementedError` the
  moment `fetch_findings` is called.
- **No ticket lifecycle beyond creation.** If a SonarQube issue is later
  resolved, nothing updates or closes the corresponding Jira ticket —
  dedupe only ever prevents *re-creating* a ticket, it never reconciles
  state in the other direction.
- **The screenshot/extraction selectors are SonarQube-version-specific.**
  They were discovered by live trial-and-error against one specific
  SonarQube Community Build version and will need re-verification after
  a SonarQube upgrade that changes its frontend markup.
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
