"""User-job authoring resolves every durable Workflow address."""

from __future__ import annotations

import pytest

from work_buddy.mcp_server import registry
from work_buddy.mcp_server.registry import Skill, WorkflowDefinition
from work_buddy.sidecar.scheduler import jobs as jobs_module
from work_buddy.sidecar.scheduler.jobs import create_user_job_file, load_jobs


WORKFLOW_ID = "wfd_11111111111111111111111111111111"
WORKFLOW_NAME = "current-flow"
WORKFLOW_ALIAS = "former-flow"


def _workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        name=WORKFLOW_NAME,
        display_name="Current Flow",
        description="Workflow address test fixture",
        workflow_file="store:test/current-flow",
        execution="main",
        workflow_id=WORKFLOW_ID,
        workflow_revision="sha256:" + "2" * 64,
        aliases=(WORKFLOW_ALIAS,),
        params_schema={
            "project": {
                "type": "str",
                "description": "Project name",
                "required": True,
            }
        },
    )


def _install_registry(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    workflow = _workflow()
    calls: list[str] = []

    monkeypatch.setattr(registry, "get_registry", lambda: {WORKFLOW_NAME: workflow})

    def get_entry(address: str):
        calls.append(address)
        if address in {WORKFLOW_NAME, WORKFLOW_ALIAS, WORKFLOW_ID}:
            return workflow
        return None

    monkeypatch.setattr(registry, "get_entry", get_entry)
    return calls


@pytest.mark.parametrize("address", [WORKFLOW_NAME, WORKFLOW_ALIAS, WORKFLOW_ID])
def test_workflow_job_accepts_every_registry_address(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    calls = _install_registry(monkeypatch)

    result = create_user_job_file(
        tmp_path,
        name="scheduled-flow",
        schedule="0 9 * * 1-5",
        job_type="workflow",
        workflow=address,
        params={"project": "paper-a"},
    )

    assert result["success"] is True, result.get("error")
    assert calls == [address, address]
    assert load_jobs(tmp_path)[0].workflow == address


@pytest.mark.parametrize("address", [WORKFLOW_ALIAS, WORKFLOW_ID])
def test_workflow_alias_and_id_use_resolved_parameter_schema(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    calls = _install_registry(monkeypatch)

    result = create_user_job_file(
        tmp_path,
        name="invalid-flow",
        schedule="0 9 * * 1-5",
        job_type="workflow",
        workflow=address,
        params={},
    )

    assert result["success"] is False
    assert "missing required param" in result["error"]
    assert calls == [address, address]
    assert not (tmp_path / "invalid-flow.md").exists()


def test_workflow_job_rejects_omitted_required_params(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_registry(monkeypatch)

    result = create_user_job_file(
        tmp_path,
        name="missing-required-param",
        schedule="0 9 * * 1-5",
        job_type="workflow",
        workflow=WORKFLOW_ALIAS,
    )

    assert result["success"] is False
    assert "missing required param" in result["error"]
    assert calls == [WORKFLOW_ALIAS, WORKFLOW_ALIAS]
    assert not (tmp_path / "missing-required-param.md").exists()


@pytest.mark.parametrize("address", ["yes", "old # alias", "old: flow"])
def test_workflow_job_round_trips_yaml_sensitive_aliases(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    workflow = _workflow()
    workflow.aliases = (address,)
    monkeypatch.setattr(registry, "get_registry", lambda: {WORKFLOW_NAME: workflow})
    monkeypatch.setattr(
        registry,
        "get_entry",
        lambda candidate: workflow if candidate == address else None,
    )

    result = create_user_job_file(
        tmp_path,
        name="yaml-sensitive-address",
        schedule="0 9 * * 1-5",
        job_type="workflow",
        workflow=address,
        params={"project": "paper-a"},
    )

    assert result["success"] is True, result.get("error")
    assert load_jobs(tmp_path)[0].workflow == address


def test_skill_name_validation_does_not_use_workflow_address_resolution(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = Skill(
        name="canonical-skill",
        description="Skill behavior remains canonical-name based",
        category="test",
        parameters={},
        callable=lambda: None,
    )
    monkeypatch.setattr(registry, "get_registry", lambda: {skill.name: skill})
    monkeypatch.setattr(
        registry,
        "get_entry",
        lambda _address: pytest.fail("Skill validation must not use get_entry"),
    )

    accepted = create_user_job_file(
        tmp_path,
        name="canonical-skill-job",
        schedule="0 9 * * 1-5",
        job_type="skill",
        skill="canonical-skill",
    )
    rejected = create_user_job_file(
        tmp_path,
        name="skill-alias-job",
        schedule="0 9 * * 1-5",
        job_type="skill",
        skill="skill-alias",
    )

    assert accepted["success"] is True
    assert rejected["success"] is False
    assert "Unknown skill" in rejected["error"]
