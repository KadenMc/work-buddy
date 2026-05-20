"""Sidecar status and job-management ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field).
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def sidecar_status() -> dict:
    """Check whether the sidecar daemon is running and return its state."""
    from dataclasses import asdict

    from work_buddy.sidecar.pid import check_existing_daemon
    from work_buddy.sidecar.state import load_state

    state = load_state()
    if state is None:
        return {"running": False, "message": "Sidecar state file not found."}
    alive = check_existing_daemon() is not None
    data = asdict(state)
    data["running"] = alive
    return data


def sidecar_jobs() -> dict:
    """List the scheduled sidecar jobs from sidecar_state.json."""
    from dataclasses import asdict

    from work_buddy.sidecar.state import load_state

    state = load_state()
    if state is None:
        return {"jobs": [], "message": "Sidecar not running."}
    return {
        "jobs": [asdict(j) for j in state.jobs],
        "exclusion_active": state.exclusion_active,
    }


def user_job_create(
    name: str,
    schedule: str,
    job_type: str = "prompt",
    capability: str = "",
    params: dict | None = None,
    workflow: str = "",
    prompt: str = "",
    enabled: bool = True,
    recurring: bool = True,
    overwrite: bool = False,
    jitter_seconds: int = 0,
) -> dict:
    """Author a user job by writing a .md file under <data_root>/user_jobs/."""
    from work_buddy.paths import data_dir
    from work_buddy.sidecar.scheduler.jobs import create_user_job_file

    return create_user_job_file(
        data_dir("user_jobs"),
        name=name, schedule=schedule, job_type=job_type,
        capability=capability, params=params, workflow=workflow,
        prompt=prompt, enabled=enabled, recurring=recurring,
        overwrite=overwrite, jitter_seconds=jitter_seconds,
    )


def dashboard_interact(
    action: str,
    form_id: str,
    field: str = "",
    value=None,
    timeout_seconds: float = 10.0,
) -> dict:
    """Forward to the dashboard's /api/dashboard/interact endpoint.

    This is a thin HTTP wrapper because the rendezvous for form_submit /
    form_get_state lives in the dashboard process — the frontend's
    result-postback hits the dashboard and must share process memory with
    whoever opened the rendezvous. Routing through the dashboard endpoint
    keeps the whole transaction in one process.
    """
    import json as _json
    import urllib.error as _urlerr
    import urllib.request as _urlreq

    body = _json.dumps({
        "action": action,
        "form_id": form_id,
        "field": field,
        "value": value,
        "timeout_seconds": timeout_seconds,
    }).encode("utf-8")
    # Submit/get_state can block for the full timeout in the dashboard. Add a
    # small buffer so HTTP doesn't time out before the capability does.
    http_timeout = max(15.0, float(timeout_seconds) + 5.0)
    req = _urlreq.Request(
        "http://localhost:5127/api/dashboard/interact",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _urlreq.urlopen(req, timeout=http_timeout) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except _urlerr.HTTPError as exc:
        try:
            return _json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"dashboard returned HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "error": f"dashboard unreachable: {exc}"}


def _register() -> None:
    register_op("op.wb.sidecar_status", sidecar_status)
    register_op("op.wb.sidecar_jobs", sidecar_jobs)
    register_op("op.wb.user_job_create", user_job_create)
    register_op("op.wb.dashboard_interact", dashboard_interact)


_register()
