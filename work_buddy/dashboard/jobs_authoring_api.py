"""Human-authorized React Jobs form; submission reuses the existing capability.

The form/assistant does not write job files or invent a scheduling authority.
Legacy management remains available during the authoring migration window.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from flask import Blueprint, jsonify, request

from work_buddy.dashboard import local_identity_api
from work_buddy.security.local_identity import LocalIdentityError
from work_buddy.truth.identity import canonical_json, sha256_text


def create_user_job(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Shared manual-form submit path, including the existing validation/events."""
    from work_buddy.mcp_server.registry import get_registry

    cap = get_registry().get("user_job_create")
    if cap is None:
        return {"success": False, "error": "Job creation is temporarily unavailable."}
    try:
        result = cap.callable(**dict(payload))
    except TypeError as exc:
        return {"success": False, "error": f"Invalid arguments: {exc}"}
    if result.get("success"):
        from work_buddy.dashboard.events import publish_auto

        publish_auto("user_job.created", {"name": result.get("name"), "file_path": result.get("file_path")})
    return result


def create_jobs_authoring_blueprint(
    *,
    create_job: Callable[[Mapping[str, Any]], dict[str, Any]] = create_user_job,
    authorizer: Callable[[Mapping[str, Any]], str] | None = None,
    read_only: Callable[[], bool] | None = None,
) -> Blueprint:
    bp = Blueprint("jobs_authoring", __name__)

    def configured_read_only() -> bool:
        if read_only is not None:
            return read_only()
        from work_buddy.config import load_config

        return load_config().get("dashboard", {}).get("read_only", False) is True

    @bp.get("/api/jobs/authoring")
    def view():
        from work_buddy.config import load_config

        access: dict[str, str] = {"mode": "read_write"}
        try:
            if authorizer is None:
                local_identity_api.authenticate_request_session()
            if configured_read_only():
                access = {"mode": "read_only", "reason": "The dashboard is read-only."}
        except LocalIdentityError:
            access = {"mode": "read_only", "reason": "Open the dashboard from the Work Buddy tray to authorize job creation."}
        response = jsonify({"ok": True, "access": access, "time_zone": load_config().get("timezone", "local time")})
        response.headers["Cache-Control"] = "no-store"
        return response

    @bp.post("/api/jobs/authoring")
    def submit():
        if configured_read_only():
            return jsonify({"success": False, "error": "The dashboard is read-only."}), 403
        if request.content_length is not None and request.content_length > 128 * 1024:
            return jsonify({"success": False, "error": "The job form is too large."}), 413
        body = request.get_json(silent=True)
        allowed = {"client_mutation_id", "name", "schedule", "job_type", "capability", "workflow", "prompt", "params", "jitter_seconds"}
        if not isinstance(body, dict) or set(body) - allowed:
            return jsonify({"success": False, "error": "Invalid job form fields. This form cannot overwrite existing jobs."}), 400
        mutation_id = body.get("client_mutation_id")
        if not isinstance(mutation_id, str) or not mutation_id or len(mutation_id) > 200:
            return jsonify({"success": False, "error": "A job form request ID is required."}), 400
        try:
            if authorizer is not None:
                authorizer(body)
            else:
                local_identity_api.require_human_authority_request(
                    action="dashboard.jobs.create", subject=f"job:new:{mutation_id}",
                    context_sha256=sha256_text(canonical_json({"method": "POST", "path": request.path, "body": body})),
                )
        except LocalIdentityError as exc:
            return jsonify({"success": False, "code": exc.code, "error": str(exc)}), exc.status
        payload = {key: value for key, value in body.items() if key != "client_mutation_id"}
        if any(not isinstance(payload.get(key, ""), str) for key in ("name", "schedule", "job_type", "capability", "workflow", "prompt")):
            return jsonify({"success": False, "error": "Job text fields must be text."}), 400
        if "params" in payload and not isinstance(payload["params"], dict):
            return jsonify({"success": False, "error": "Parameters must be a JSON object.", "errors_by_field": {"params": "Use a JSON object."}}), 400
        jitter = payload.get("jitter_seconds", 0)
        if isinstance(jitter, bool) or not isinstance(jitter, int) or jitter < 0:
            return jsonify({"success": False, "error": "Jitter must be a non-negative integer.", "errors_by_field": {"jitter_seconds": "Use a non-negative whole number."}}), 400
        from work_buddy.sidecar.scheduler.cron import (
            compute_max_jitter_seconds,
            cron_interval_seconds,
        )

        maximum = compute_max_jitter_seconds(cron_interval_seconds(payload.get("schedule", "")))
        if jitter > maximum:
            message = f"Jitter cannot exceed {maximum} seconds for this schedule."
            return jsonify({"success": False, "error": message, "errors_by_field": {"jitter_seconds": message}}), 400
        from work_buddy.consent import user_initiated

        with user_initiated("dashboard.jobs.create"):
            result = create_job({**payload, "overwrite": False})
        return jsonify(result), 200 if result.get("success") else 400

    return bp
