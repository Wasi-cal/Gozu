"""
Flask webhook receiver for SonarQube.

Flow: webhook fires -> verify HMAC signature -> kick off a Temporal
workflow with the project key -> return 200 immediately (the actual
fetch/log work happens asynchronously in the workflow/activity).
"""

import asyncio
import hashlib
import hmac
import logging
import os
import uuid

from flask import Flask, jsonify, request
from temporalio.client import Client

from workflows import SonarToJiraWorkflow

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

TASK_QUEUE = "sonar-jira-queue"
TEMPORAL_ADDRESS = "localhost:7233"


def verify_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """
    SonarQube signs the raw request body with HMAC-SHA256 using the webhook
    secret, and sends the hex digest in X-Sonar-Webhook-HMAC-SHA256.
    """
    if not signature_header:
        return False

    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


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
        asyncio.run(start_workflow(project_key, task_id))
    except Exception:
        logger.exception("Failed to start Temporal workflow")
        return jsonify({"error": "failed to start workflow"}), 500

    return jsonify({"status": "accepted", "project_key": project_key, "task_id": task_id}), 200


async def start_workflow(project_key: str, task_id: str):
    client = await Client.connect(TEMPORAL_ADDRESS)
    workflow_id = f"sonar-to-jira-{project_key}-{task_id}"

    await client.start_workflow(
        SonarToJiraWorkflow.run,
        {"project_key": project_key, "task_id": task_id},
        id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info(f"Started workflow {workflow_id}")


if __name__ == "__main__":
    app.run(port=5001, debug=True)
