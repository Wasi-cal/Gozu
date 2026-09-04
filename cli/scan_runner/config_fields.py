"""Small derived values read from a config dict, shared by scanner_exec.py and workflow_trigger.py."""


def scanner_host_url(config: dict) -> str:
    """SonarQube Cloud is always sonarcloud.io; "local" uses whatever host URL the config was given."""
    return config["credentials"]["sonar_host_url"] if config["scanner_mode"] == "local" else "https://sonarcloud.io"


def require_project_key(config: dict) -> str:
    project_key = config.get("project_key")
    if not project_key:
        raise RuntimeError(
            f"Config '{config['name']}' has no project_key (it predates that field) - "
            "recreate it with `gozu init` before running a scan."
        )
    return project_key


def config_branches(config: dict) -> list[str]:
    """Empty list means "no restriction" (matches config.store/receiver/app.py), not "zero branches"."""
    raw = config.get("branches")
    if not raw:
        return []
    return [b.strip() for b in raw.split(",") if b.strip()]
