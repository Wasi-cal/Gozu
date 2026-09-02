-- Mounted into the postgres container at /docker-entrypoint-initdb.d/, so
-- this runs automatically the first time the postgres data volume is
-- initialized (NOT on every container start - see README's Phase 1 section
-- if you need to re-run this against an existing volume).

-- A "config" is one named set of scanner + ticket-backend credentials.
-- Called "configs", not "profiles" - Docker Compose already has an
-- unrelated concept called profiles (for conditionally starting services),
-- and reusing that word here throughout the codebase/docs would be
-- confusing.
CREATE TABLE IF NOT EXISTS configs (
    id                SERIAL PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,

    scanner_type      TEXT NOT NULL,              -- e.g. "sonarqube"
    scanner_mode      TEXT NOT NULL,               -- "local" or "cloud"
    sonar_host_url    TEXT,
    sonar_token       TEXT,                        -- Fernet-encrypted ciphertext, never plaintext
    sonar_organization TEXT,

    ticket_backend    TEXT NOT NULL DEFAULT 'jira',
    jira_url          TEXT,
    jira_email        TEXT,
    jira_api_token    TEXT,                        -- Fernet-encrypted ciphertext, never plaintext
    jira_project_key  TEXT,

    trigger_mode      TEXT NOT NULL,               -- "direct", "webhook", or "watch"
    webhook_secret    TEXT,                        -- Fernet-encrypted ciphertext, never plaintext

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Lookups are by name (config_store.get_config(name), delete_config(name)).
-- No separate CREATE INDEX needed: the UNIQUE constraint on `name` above
-- already creates a unique b-tree index that covers exact-match lookups.
