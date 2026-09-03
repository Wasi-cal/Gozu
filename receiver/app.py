"""
Flask webhook receiver for SonarQube - multi-config routing (Phase 4).

Flow: SonarQube POSTs to /webhooks/sonarqube/<config_name> -> load that
config, verify the signature against *its own* webhook_secret, confirm the
payload's project matches the config, then trigger ScanToTicketWorkflow and
return 200 immediately. No SonarQube ce/task polling here (unlike direct
invocation, cli/scan_runner.py) - the webhook firing at all already means
SonarQube's server-side processing finished; that's what it's signaling.
"""

import asyncio
import fnmatch
import logging

from flask import Flask, jsonify, request

import config.store as config_store
from receiver.starter import start_scan_to_ticket_workflow
from receiver.verify_signature import verify_signature

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class _HealthCheckLogFilter(logging.Filter):
    """
    Drops werkzeug's per-request access log line for GET /health - Docker
    polls it every 5s (docker-compose.yml's receiver healthcheck), which
    would otherwise bury real webhook activity in noise.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "/health" not in record.getMessage()


logging.getLogger("werkzeug").addFilter(_HealthCheckLogFilter())

# No CSRF protection needed: this app has no cookies/sessions/HTML forms for
# an attacker's page to ride on. Each route is authenticated by an
# HMAC signature keyed to the specific config in the URL (verify_signature.py),
# which a forged cross-site request can't produce without that config's
# webhook_secret.
app = Flask(__name__)


@app.route("/health")
def health():
    """For docker-compose.yml's healthcheck - just confirms the process is up and serving."""
    return jsonify({"status": "ok"}), 200


@app.route("/webhooks/sonarqube/<config_name>", methods=["POST"])
def sonarqube_webhook(config_name: str):
    config = config_store.get_config(config_name)
    if config is None:
        logger.warning(f"Rejected webhook: no config named '{config_name}'")
        return jsonify({"error": f"no config named '{config_name}'"}), 404

    webhook_secret = config["credentials"].get("webhook_secret")
    if not webhook_secret:
        logger.error(f"Config '{config_name}' has no webhook_secret configured")
        return jsonify({
            "error": f"config '{config_name}' has no webhook_secret - run `codescan up` to generate one",
        }), 500

    signature = request.headers.get("X-Sonar-Webhook-HMAC-SHA256", "")
    raw_body = request.get_data()
    if not verify_signature(raw_body, signature, webhook_secret):
        logger.warning(f"Rejected webhook for '{config_name}': invalid or missing HMAC signature")
        return jsonify({"error": "invalid signature"}), 401

    payload = request.get_json(silent=True) or {}

    status = payload.get("status")
    if status != "SUCCESS":
        logger.info(f"Ignoring webhook for '{config_name}' with status={status!r}")
        return jsonify({"status": "ignored", "reason": f"status={status!r}"}), 200

    payload_project_key = payload.get("project", {}).get("key")
    if payload_project_key != config["project_key"]:
        logger.warning(
            f"Rejected webhook for '{config_name}': payload project.key={payload_project_key!r} "
            f"doesn't match the config's project_key={config['project_key']!r} "
            "(webhook pointed at the wrong config?)"
        )
        return jsonify({"error": "project key mismatch"}), 400

    task_id = payload.get("taskId")
    if not task_id:
        logger.warning(f"Rejected webhook for '{config_name}': missing taskId in payload")
        return jsonify({"error": "missing taskId in payload"}), 400

    branch = payload.get("branch", {}).get("name")

    # No fan-out logic needed here (unlike direct invocation's
    # MultiBranchScanWorkflow) - SonarQube already delivers one webhook per
    # branch, so a multi-branch config's `branches` list just gates which
    # of those deliveries proceed. Null/empty branches means "no
    # restriction" (matches config/store.py/cli/scan_runner/).
    config_branches = config.get("branches")
    if config_branches:
        patterns = [b.strip() for b in config_branches.split(",") if b.strip()]
        if not any(fnmatch.fnmatch(branch or "", pattern) for pattern in patterns):
            logger.info(
                f"Skipping webhook for '{config_name}': branch {branch!r} doesn't match "
                f"tracked branches {patterns}"
            )
            return jsonify({"status": "ignored", "reason": f"branch {branch!r} not tracked"}), 200

    logger.info(f"Verified webhook for config='{config_name}' project_key={payload_project_key} task_id={task_id}")

    try:
        workflow_id = asyncio.run(start_scan_to_ticket_workflow(config, task_id, branch))
    except Exception:
        logger.exception("Failed to start Temporal workflow")
        return jsonify({"error": "failed to start workflow"}), 500

    logger.info(f"Started workflow {workflow_id}")
    return jsonify({"status": "accepted", "config": config_name, "task_id": task_id}), 200


if __name__ == "__main__":
    # Always binds 5000 internally - RECEIVER_PORT (.env) is only the
    # host-side port mapping in docker-compose.yml, not this process's own.
    app.run(host="0.0.0.0", port=5000)
