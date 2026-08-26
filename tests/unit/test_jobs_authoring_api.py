from __future__ import annotations

from unittest.mock import Mock

import pytest
from flask import Flask

from work_buddy.dashboard.jobs_authoring_api import create_jobs_authoring_blueprint
from work_buddy.sidecar.scheduler.jobs import create_user_job_file


def client(tmp_path, **options):
    app = Flask(__name__)
    app.register_blueprint(create_jobs_authoring_blueprint(
        create_job=lambda body: create_user_job_file(tmp_path, **body),
        authorizer=options.pop("authorizer", lambda body: "user:test"),
        read_only=options.pop("read_only", lambda: False),
    ))
    return app.test_client()


def payload(**changes):
    return {"client_mutation_id": "job-click-1", "name": "isolated-test", "schedule": "0 9 * * 1", "job_type": "prompt", "prompt": "A test only.", "jitter_seconds": 0, **changes}


def test_click_uses_existing_scheduler_validation_and_never_overwrites(tmp_path):
    browser = client(tmp_path)
    assert browser.post("/api/jobs/authoring", json=payload()).status_code == 200
    original = (tmp_path / "isolated-test.md").read_text()
    assert browser.post("/api/jobs/authoring", json=payload(prompt="Changed")).status_code == 400
    assert (tmp_path / "isolated-test.md").read_text() == original
    assert len(list(tmp_path.glob("*.md"))) == 1


@pytest.mark.parametrize("changes", [
    {"overwrite": True}, {"params": []}, {"jitter_seconds": True},
    {"jitter_seconds": 1.5}, {"jitter_seconds": -1}, {"jitter_seconds": 301},
    {"schedule": "invalid"}, {"name": "../outside"}, {"client_mutation_id": ""},
])
def test_invalid_or_privileged_fields_cannot_create_a_job(tmp_path, changes):
    response = client(tmp_path).post("/api/jobs/authoring", json=payload(**changes))
    assert response.status_code == 400
    assert not list(tmp_path.glob("*.md"))


def test_read_only_blocks_before_create(tmp_path):
    authorize = Mock(return_value="user:test")
    response = client(tmp_path, authorizer=authorize, read_only=lambda: True).post("/api/jobs/authoring", json=payload())
    assert response.status_code == 403
    authorize.assert_not_called()
    assert not list(tmp_path.glob("*.md"))


def test_real_boundary_requires_local_human_authority(tmp_path):
    response = client(tmp_path, authorizer=None).post("/api/jobs/authoring", json=payload())
    assert response.status_code in {401, 403}
    assert not list(tmp_path.glob("*.md"))


def test_retired_jobs_form_bridge_never_broadcasts_or_submits(monkeypatch):
    from work_buddy.dashboard.interact import dashboard_interact
    publish = Mock()
    monkeypatch.setattr("work_buddy.dashboard.events.publish_auto", publish)
    for action in ("form_open", "form_field_set", "form_submit", "form_get_state"):
        assert dashboard_interact(action, "jobs-add-job", field="name", value="test")["code"] == "form_migrated"
    publish.assert_not_called()
