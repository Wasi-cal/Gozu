"""
Flask webhook receiver for SonarQube.

Flow: webhook fires -> verify HMAC signature -> kick off a Temporal
workflow with the project key -> return 200 immediately (the actual
fetch/log work happens asynchronously in the workflow/activity).
"""

import asyncio
import logging
import os
import uuid

from flask import Flask, jsonify, request

from receiver.starter import start_scan_to_ticket_workflow
from receiver.verify_signature import verify_signature

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.route("/webhooks/sonarqube", methods=["POST"])
def sonarqube_webhook():
    secret = os.environ.get("SONAR_WEBHOOK_SECRET", "")
    signature = request.headers.get("X-Sonar-Webhook-HMAC-SHA256", "")
    raw_body = request.get_data()

    if not verify_signature(raw_body, signature, secret):
        logger.warning("Rejected webhook: invalid or missing HMAC signature")
        return jsonify({"error": "invalid signature"}), 401

    payload = request.get_json(silent=True) or {}
    project_key = payload.get("project", {}).get("key")

    if not project_key:
        logger.warning("Rejected webhook: missing project.key in payload")
        return jsonify({"error": "missing project.key in payload"}), 400

    task_id = payload.get("taskId", str(uuid.uuid4()))

    logger.info(f"Verified webhook for project_key={project_key} task_id={task_id}")

    try:
        workflow_id = asyncio.run(start_scan_to_ticket_workflow(project_key, task_id))
    except Exception:
        logger.exception("Failed to start Temporal workflow")
        return jsonify({"error": "failed to start workflow"}), 500

    logger.info(f"Started workflow {workflow_id}")

    return jsonify({"status": "accepted", "project_key": project_key, "task_id": task_id}), 200


if __name__ == "__main__":
    app.run(port=5001, debug=True)
