# Running gozu on a new machine

This is the quick path for getting `gozu` installed and scanning a project
on any machine — yours, a friend's, whatever. For how the tool works
internally, see `ARCHITECTURE.md`. For the source-checkout developer flow
(`git clone` + `uv sync`), see `README.md`.

## 1. Requirements

- **Docker Desktop** (or Docker Engine + Compose), installed and running.
  Postgres, Temporal, and (if scanning locally) SonarQube all run as
  containers — nothing here needs a remote server.
- **Python 3.12+**.
- Jira access ready to paste in during setup: a Cloud API token, the
  account email it belongs to, and the target project key. You do **not**
  need Java or `sonar-scanner` installed yourself — `gozu` downloads
  portable copies into `~/.gozu/` the first time it needs them.

## 2. Install

```bash
pipx install https://github.com/Wasi-cal/Gozu/releases/download/v0.3.0/gozu-0.3.0-py3-none-any.whl
```

Swap `v0.3.0` for whatever the latest tag is on the
[releases page](https://github.com/Wasi-cal/Gozu/releases). Don't have
`pipx`? Plain `pip install <that same url>` into a virtualenv works too.

(This isn't on PyPI yet, so `pipx install gozu` doesn't work — installing
from the release URL above is the current path.)

## 3. Set up and run

```bash
gozu init      # wizard: .env + Docker stack setup, SonarQube Local/Cloud, Jira creds
gozu up        # brings up whatever that config needs
gozu run       # scans the current directory, creates Jira tickets for new findings
```

- Point it at a specific project: `gozu run --path /path/to/project`, or
  just `cd` there first and run `gozu run` with no flags.
- On SonarQube Cloud's **Free** plan, add `--watch` — Free has no webhook
  to receive, so this polls on an interval instead.
- Adding a second project later: run `gozu init` again. If you already
  have a saved Jira ticket destination, the wizard offers to reuse it
  instead of asking for credentials a second time.

## 4. Stopping / resetting

- `gozu down` — stops the containers; your configs and data persist.
- `gozu down --wipe` — the destructive one-shot: deletes configs, ticket
  destinations, and dedupe state (with a pre-wipe Postgres backup written
  to `~/.gozu/backups/` first). Use this only if you actually want a clean
  slate.
