-- Copyright (c) 2026 Calfus Inc.
-- Author: Wasiullah Rafeeq S
-- Editor: Prakrit Mohanty

-- Mounted into the postgres container at /docker-entrypoint-initdb.d/, so
-- this runs automatically the first time the postgres data volume is
-- initialized (NOT on every container start - see README's Phase 1 section
-- if you need to re-run this against an existing volume).

-- A "config" is one named set of scanner + ticket-backend credentials.
-- Called "configs", not "profiles" - Docker Compose already has an
-- unrelated concept called profiles (for conditionally starting services),
-- and reusing that word here throughout the codebase/docs would be
-- confusing.
--
-- Deliberately normalized: `configs` only holds the fields every backend
-- has in common (which scanner, local/cloud, which ticket backend, how
-- it's triggered). Anything backend-specific (a Sonar host URL, a Jira
-- project key, whatever a future scanner/ticket backend needs) lives in
-- config_credentials as arbitrary key/value pairs instead of being baked
-- in as dedicated columns - adding a new scanner or ticket backend never
-- requires an ALTER TABLE here, matching how ScannerClient/TicketClient
-- are themselves generic over the backend.
CREATE TABLE IF NOT EXISTS configs (
    id             SERIAL PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    scanner_type   TEXT NOT NULL,               -- e.g. "sonarqube"
    scanner_mode   TEXT NOT NULL,                -- "local" or "cloud"
    ticket_backend TEXT NOT NULL DEFAULT 'jira',
    trigger_mode   TEXT NOT NULL,                -- "direct", "webhook", "watch", "github_poll", ...
    project_key    TEXT,                         -- nullable: configs created before Phase 3 predate this
    sonar_plan     TEXT,                         -- "free" or "premium" - Cloud only; null for Local or pre-Phase-5 configs
    branches       TEXT,                         -- comma-separated (e.g. "main,release/2.0"); null/empty = no restriction
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Phase 3 migration for a volume that already ran the CREATE TABLE above
-- without `project_key` - CREATE TABLE IF NOT EXISTS is a no-op on an
-- existing table, so this is what actually adds the column to it. Nullable
-- at the DB level so pre-existing configs aren't broken by a NOT NULL
-- constraint; the CLI wizard still treats it as a required prompt for any
-- new config (see cli/init_wizard.py) - `gozu run` needs it (no more
-- webhook payload to pull it from), so a config created before this
-- migration must be recreated via `gozu init` before it can be used
-- with `gozu run`.
ALTER TABLE configs ADD COLUMN IF NOT EXISTS project_key TEXT;

-- Phase 5 migration, same reasoning as project_key above: nullable so a
-- pre-existing config isn't broken, and treated as "no restriction"
-- wherever it's read (config/store.py, receiver/app.py, cli/scan_runner/)
-- rather than an error - not every scanner_mode has a "plan" concept
-- (Local configs leave sonar_plan null), and not every config tracks more
-- than one branch (a null/empty branches list means "any branch", not
-- "no branches").
ALTER TABLE configs ADD COLUMN IF NOT EXISTS sonar_plan TEXT;
ALTER TABLE configs ADD COLUMN IF NOT EXISTS branches TEXT;

-- Lookups are by name (config/store.py's get_config(name), delete_config(name)).
-- No separate CREATE INDEX needed: the UNIQUE constraint on `name` above
-- already creates a unique b-tree index that covers exact-match lookups.

-- Every credential/setting value is Fernet-encrypted ciphertext (see
-- config/crypto.py), never plaintext - key names are backend-specific
-- strings (sonar_host_url, sonar_token, sonar_organization, jira_url,
-- jira_email, jira_api_token, jira_project_key, webhook_secret, ...),
-- not fixed columns.
CREATE TABLE IF NOT EXISTS config_credentials (
    id         SERIAL PRIMARY KEY,
    config_id  INTEGER NOT NULL REFERENCES configs(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    UNIQUE (config_id, key)
);

-- A ticket destination is one shared board + credentials (today: a Jira
-- project + its URL/email/API token) that more than one config can point
-- at, instead of each config carrying its own copy of the same
-- credentials. Purely additive, same philosophy as project_key/sonar_plan/
-- branches above: `configs.ticket_destination_id` is nullable and left
-- NULL on every config that already exists - config/store.py's
-- get_config() falls back to reading Jira fields straight out of
-- config_credentials (the original, still-supported shape) whenever it's
-- NULL, and only resolves them from these tables when it's set. No
-- backfill migration - a pre-existing config's embedded credentials are
-- left exactly as they are.
CREATE TABLE IF NOT EXISTS ticket_destinations (
    id             SERIAL PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    ticket_backend TEXT NOT NULL DEFAULT 'jira',
    project_key    TEXT NOT NULL,  -- the Jira project key, not a secret
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Same shape as config_credentials: Fernet-encrypted arbitrary key/value
-- pairs (jira_url, jira_email, jira_api_token), not fixed columns.
CREATE TABLE IF NOT EXISTS ticket_destination_credentials (
    id             SERIAL PRIMARY KEY,
    destination_id INTEGER NOT NULL REFERENCES ticket_destinations(id) ON DELETE CASCADE,
    key            TEXT NOT NULL,
    value          TEXT NOT NULL,
    UNIQUE (destination_id, key)
);

ALTER TABLE configs ADD COLUMN IF NOT EXISTS ticket_destination_id
    INTEGER REFERENCES ticket_destinations(id);

-- Idempotency ledger for ticket creation (ticket/claims.py). Closes a
-- check-then-act race in create_tickets_activity: without this, two
-- overlapping runs (concurrent multi-branch fan-out hitting the same
-- underlying project, an activity retry after a partial failure, or two
-- configs pointed at the same SonarQube + Jira project) could each search
-- Jira, each see no existing ticket, and each create one - a duplicate.
-- `destination` scopes this per ticket backend + project (e.g.
-- "jira:https://x.atlassian.net:PROJ", see TicketClient.destination_id()),
-- so two genuinely different destinations tracking the same finding_key
-- don't collide with each other.
--
-- A row with ticket_key still NULL means a claim is in progress; ticket/
-- claims.py's claim() self-heals a crashed attempt by freeing a claim
-- that's been NULL for too long, rather than blocking that finding forever.
CREATE TABLE IF NOT EXISTS ticket_claims (
    destination  TEXT NOT NULL,
    finding_key  TEXT NOT NULL,
    ticket_key   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (destination, finding_key)
);
