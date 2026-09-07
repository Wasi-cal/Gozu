-- Copyright (c) 2026 Calfus Inc.
-- Author: Wasiullah Rafeeq S
--
-- Depends on: PostgreSQL - data storage; SonarQube (SonarSource) - JDBC backend for local-mode SonarQube

-- Runs once, at first init of a fresh postgres_data volume (same as
-- init.sql alongside it, which docker-entrypoint-initdb.d also mounts) -
-- creates a genuinely separate database + role for local-mode SonarQube's
-- own Postgres backend (docker-compose.yml's SONAR_JDBC_* vars on the
-- sonarqube service). A real second database, not a schema within gozu's
-- own - fully isolated, so `gozu down --wipe`'s DROP DATABASE against
-- gozu's own database (cli/stack/wipe.py) can never reach this one.
--
-- A plain .sql file, not a .sh script - deliberately: a *.sh file's
-- shell/exec-bit mechanics (docker-entrypoint.sh runs it directly if
-- marked executable, else sources it) turned out NOT to survive being
-- copied into ~/.gozu/stack/ (cli/stack/files.py strips the executable
-- bit) and, separately, Docker Desktop's macOS bind-mount layer reported
-- the executable bit inconsistently anyway - confirmed live, it failed
-- with "bad interpreter: Permission denied". A .sql file sidesteps all of
-- that: docker-entrypoint-initdb.d always runs *.sql through psql
-- directly, no executable bit involved at all. \getenv (psql 10+) is what
-- lets a .sql file still read SONARQUBE_DB_PASSWORD from the container's
-- own environment (set via docker-compose.yml) despite .sql files having
-- no shell variable substitution of their own.
\getenv sonarqube_db_password SONARQUBE_DB_PASSWORD

CREATE ROLE sonarqube WITH LOGIN PASSWORD :'sonarqube_db_password';
CREATE DATABASE sonarqube OWNER sonarqube;
