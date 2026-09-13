from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from work_buddy.dashboard import service


@pytest.fixture
def client(authenticate_dashboard_client):
    service.app.config["TESTING"] = True
    with service.app.test_client() as test_client:
        authenticate_dashboard_client(test_client)
        yield test_client


def test_registry_list_exposes_the_canonical_jobs_catalog(client, monkeypatch):
    projection = {
        "skills": [
            {
                "name": "journal_state",
                "description": "Read Journal state.",
                "parameters": [],
                "slash_command": "",
            }
        ],
        "workflows": [],
    }
    monkeypatch.setattr(
        "work_buddy.dashboard.job_registry.job_registry_projection",
        lambda: projection,
    )

    response = client.get("/api/registry/list")

    assert response.status_code == 200
    assert response.get_json() == projection
    assert "capabilities" not in response.get_json()


def test_user_job_get_normalizes_a_persisted_capability_file(
    client, tmp_path, monkeypatch
):
    jobs_dir = tmp_path / "user_jobs"
    jobs_dir.mkdir()
    (jobs_dir / "legacy-direct.md").write_text(
        "---\nschedule: \"0 9 * * *\"\ntype: capability\n"
        "capability: journal_state\nparams: {}\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "work_buddy.paths.data_dir",
        lambda *parts: tmp_path.joinpath(*parts),
    )

    response = client.get("/api/user_jobs/legacy-direct")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["job_type"] == "skill"
    assert payload["skill"] == "journal_state"
    assert "capability" not in payload


def test_user_job_post_accepts_cached_direct_skill_fields_and_forwards_canonical(
    client, monkeypatch
):
    create = Mock(
        side_effect=lambda **kwargs: {"success": True, **kwargs},
    )
    monkeypatch.setattr(
        "work_buddy.mcp_server.registry.get_registry",
        lambda: {"user_job_create": SimpleNamespace(callable=create)},
    )
    monkeypatch.setattr("work_buddy.dashboard.events.publish_auto", Mock())

    response = client.post(
        "/api/user_jobs",
        json={
            "name": "cached-direct-skill",
            "schedule": "0 9 * * *",
            "job_type": "capability",
            "capability": "journal_state",
            "params": {},
        },
    )

    assert response.status_code == 200
    create.assert_called_once_with(
        name="cached-direct-skill",
        schedule="0 9 * * *",
        job_type="skill",
        skill="journal_state",
        params={},
    )
    assert response.get_json()["job_type"] == "skill"
    assert response.get_json()["skill"] == "journal_state"
    assert "capability" not in response.get_json()
