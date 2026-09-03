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
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Phase 3 migration for a volume that already ran the CREATE TABLE above
-- without `project_key` - CREATE TABLE IF NOT EXISTS is a no-op on an
-- existing table, so this is what actually adds the column to it. Nullable
-- at the DB level so pre-existing configs aren't broken by a NOT NULL
-- constraint; the CLI wizard still treats it as a required prompt for any
-- new config (see cli/init_wizard.py) - `codescan run` needs it (no more
-- webhook payload to pull it from), so a config created before this
-- migration must be recreated via `codescan init` before it can be used
-- with `codescan run`.
ALTER TABLE configs ADD COLUMN IF NOT EXISTS project_key TEXT;

-- Lookups are by name (config_store.get_config(name), delete_config(name)).
-- No separate CREATE INDEX needed: the UNIQUE constraint on `name` above
-- already creates a unique b-tree index that covers exact-match lookups.

-- Every credential/setting value is Fernet-encrypted ciphertext (see
-- crypto_utils.py), never plaintext - key names are backend-specific
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
