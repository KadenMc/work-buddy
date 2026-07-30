"""Criterion-first Co-work Verify configuration behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.agent_execution.models import AgentExecutionSelection
from work_buddy.cowork.verify import (
    CheckDefinitionVersion,
    CriterionActivation,
    CriterionCheckBinding,
    CriterionDefinitionVersion,
    VerifyInvariantViolation,
    seed_terminology_exact_match,
)
from work_buddy.cowork.verify import store as verify_store
from work_buddy.cowork.verify_configuration import (
    CONFIGURATION_SCHEMA,
    MAX_USER_CHECK_EVALUATION_INSTRUCTIONS_CHARS,
    create_user_criterion_draft,
    create_user_verification_check,
    list_effective_verification_configuration,
    set_document_criterion_enabled,
)
from work_buddy.cowork.verify_execution import (
    VERIFY_EXECUTION_PLAN_SCHEMA,
)
from work_buddy.truth import documents
from work_buddy.truth.contracts import Actor
from work_buddy.truth.export import export_store, import_store
from work_buddy.truth.identity import canonical_json, new_id, sha256_text

from .conftest import HUMAN, NOW


SYSTEM = Actor("system", "configuration-test")
LATER = "2026-07-17T13:00:00.000+00:00"


class _EmptyRegistry:
    def paths_for_store_id(self, _store_id: str):
        return ()


def _document(store_ctx):
    return documents.register_document(
        store_ctx["store"],
        path="docs/verify-configuration.md",
        title="Verify configuration",
        document_class="co_authored",
        content_sha256=sha256_text("# Configuration\n"),
        actor=HUMAN,
        at=NOW,
    )


def _criterion(projection, stable_key: str):
    return next(
        item
        for item in projection["criteria"]
        if item["stable_key"] == stable_key
    )


def test_seeded_criterion_projects_method_origin_limits_and_sharing(store_ctx):
    document = _document(store_ctx)

    projection = list_effective_verification_configuration(
        store_ctx["store"],
        document_id=document.id,
    )

    assert projection["schema"] == CONFIGURATION_SCHEMA
    execution_plan = projection["execution_plan"]
    assert execution_plan["schema"] == VERIFY_EXECUTION_PLAN_SCHEMA
    assert execution_plan["authoritative"] is True
    assert execution_plan["checker"] == {
        "execution_class": "in_process",
        "mechanism": "deterministic_exact_match",
        "model_call": False,
        "external_egress": False,
        "content_boundary": "captured_target",
    }
    assert execution_plan["coordination"]["selection"] == {
        "mode": "explicit_at_run_start",
        "provider_id": None,
        "model_id": None,
        "provider_label": None,
        "model_label": None,
    }
    assert execution_plan["coordination"]["content_boundary"] == (
        "entire_frozen_document"
    )
    assert execution_plan["coordination"]["external_egress"] is True
    assert execution_plan["coordination"]["fallback"] == {
        "provider_model_fallback": False,
        "failure_mode": "fail_closed",
    }
    assert execution_plan["coordination"]["worker_sessions"] == {
        "initial": 1,
        "maximum": 3,
        "conditional_roles": ["reviser", "post_revision_coordinator"],
    }
    assert execution_plan["coordination"]["cost_control"] is None
    costs = {
        item["provider_id"]: item
        for item in execution_plan["coordination"]["provider_cost_controls"]
    }
    assert costs["claude-code"] == {
        "provider_id": "claude-code",
        "enforcement_class": "hard_ceiling",
        "ceiling_usd_per_worker_session": 2.0,
        "basis": "claude_code_max_budget_usd",
    }
    assert costs["codex"] == {
        "provider_id": "codex",
        "enforcement_class": "unavailable",
        "ceiling_usd_per_worker_session": None,
        "basis": "codex_worker_has_no_budget_enforcement",
    }
    assert projection["coordination"]["deprecated"] is True
    assert projection["coordination"]["authoritative_projection"] == (
        "execution_plan"
    )
    assert projection["coordination"]["cost_ceiling_semantics"] == (
        "requested_launch_budget_not_provider_guarantee"
    )
    terminology = _criterion(projection, "terminology_exact_match")
    assert terminology["version"] == 1
    assert terminology["author_origin"]["definition_origin"] == "system"
    assert terminology["author_origin"]["author"]["kind"] == "system"
    assert terminology["effective_activation"] == {
        "id": terminology["effective_activation"]["id"],
        "enabled": True,
        "required": False,
        "locked": False,
        "scope": {"kind": "document"},
        "origin": "system",
        "criterion_check_binding_id": terminology["checks"][0]["binding"]["id"],
        "selected_check_available": True,
        "authorized_by": {
            "kind": "system",
            "ref": "cowork-verify",
            "meta": None,
        },
    }
    assert terminology["operational_state"] == "active"
    check = terminology["checks"][0]
    assert check["method"]["mechanism"] == "deterministic"
    assert check["version"] == 1
    assert check["limitations"] == [
        "Exact, case-sensitive matching only.",
        "A match reports terminology use; it does not judge contextual intent.",
    ]
    assert check["origin"]["definition_origin"] == "system"
    assert check["data_sharing"] == {
        "class": "local_only",
        "external_egress": False,
        "basis": "admitted_deterministic_executor",
    }
    assert check["availability"]["state"] == "available"
    assert check["binding"]["selected"] is True


@pytest.mark.parametrize(
    ("selection", "enforcement_class", "ceiling"),
    (
        (
            AgentExecutionSelection(
                provider_id="claude-code",
                model_id="sonnet",
                provider_label="Claude Code",
                model_label="Sonnet",
            ),
            "hard_ceiling",
            2.0,
        ),
        (
            AgentExecutionSelection(
                provider_id="codex",
                model_id="gpt-5.6-sol",
                provider_label="Codex",
                model_label="GPT-5.6 Sol",
            ),
            "unavailable",
            None,
        ),
    ),
)
def test_execution_plan_resolves_exact_selection_and_provider_cost_control(
    store_ctx,
    selection,
    enforcement_class,
    ceiling,
):
    document = _document(store_ctx)

    projection = list_effective_verification_configuration(
        store_ctx["store"],
        document_id=document.id,
        execution_selection=selection,
    )

    coordination = projection["execution_plan"]["coordination"]
    assert coordination["selection"] == {
        "mode": "explicit_at_run_start",
        **selection.to_dict(),
    }
    assert coordination["cost_control"]["provider_id"] == (
        selection.provider_id
    )
    assert coordination["cost_control"]["enforcement_class"] == (
        enforcement_class
    )
    assert coordination["cost_control"][
        "ceiling_usd_per_worker_session"
    ] == ceiling


def test_builtin_setup_can_be_previewed_without_mutating_a_read(store_ctx):
    store = store_ctx["store"]
    document = _document(store_ctx)

    projection = list_effective_verification_configuration(
        store,
        document_id=document.id,
        ensure_system_defaults=False,
    )

    terminology = _criterion(projection, "terminology_exact_match")
    assert terminology["operational_state"] == "active"
    with store.connect() as conn:
        for table in (
            "criterion_definition_versions",
            "check_definition_versions",
            "criterion_check_bindings",
            "criterion_activations",
        ):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == 0


def test_document_override_is_append_only_idempotent_and_portable(
    store_ctx,
    tmp_path: Path,
):
    store = store_ctx["store"]
    document = _document(store_ctx)
    list_effective_verification_configuration(store, document_id=document.id)

    disabled = set_document_criterion_enabled(
        store,
        document_id=document.id,
        criterion_key="terminology_exact_match",
        enabled=False,
        actor=HUMAN,
        at=NOW,
    )
    assert disabled["changed"] is True
    effective = _criterion(
        disabled["configuration"],
        "terminology_exact_match",
    )["effective_activation"]
    assert effective["enabled"] is False
    assert effective["scope"] == {
        "kind": "document",
        "document_id": document.id,
    }
    assert effective["origin"] == "user"
    assert effective["authorized_by"]["kind"] == "human"

    repeated = set_document_criterion_enabled(
        store,
        document_id=document.id,
        criterion_key="terminology_exact_match",
        enabled=False,
        actor=HUMAN,
        at=LATER,
    )
    assert repeated["changed"] is False
    assert repeated["activation_id"] == disabled["activation_id"]
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM criterion_activations"
        ).fetchone()[0] == 2

    enabled = set_document_criterion_enabled(
        store,
        document_id=document.id,
        criterion_key="terminology_exact_match",
        enabled=True,
        actor=HUMAN,
        at=LATER,
    )
    assert enabled["changed"] is True
    assert enabled["activation_id"] != disabled["activation_id"]
    assert _criterion(
        enabled["configuration"],
        "terminology_exact_match",
    )["effective_activation"]["enabled"] is True

    exported = export_store(store, tmp_path / "configuration.jsonl")
    target = tmp_path / "restored"
    target.mkdir()
    restored = import_store(
        exported.path,
        target,
        registry=_EmptyRegistry(),
    ).store
    restored_effective = _criterion(
        list_effective_verification_configuration(
            restored,
            document_id=document.id,
            ensure_system_defaults=False,
        ),
        "terminology_exact_match",
    )["effective_activation"]
    assert restored_effective["id"] == enabled["activation_id"]
    assert restored_effective["enabled"] is True


def test_required_policy_lock_rejects_user_disable(store_ctx):
    store = store_ctx["store"]
    document = _document(store_ctx)
    seeded = seed_terminology_exact_match(store, actor=SYSTEM, at=NOW)
    payload = {
        "criterion_definition_version_id": seeded.criterion.id,
        "criterion_check_binding_id": seeded.binding.id,
        "scope": {"kind": "document"},
        "is_enabled": True,
        "is_required": True,
        "origin": "system",
    }
    locked = CriterionActivation(
        id=new_id(),
        criterion_definition_version_id=seeded.criterion.id,
        criterion_check_binding_id=seeded.binding.id,
        scope_json=canonical_json(payload["scope"]),
        is_enabled=1,
        is_required=1,
        origin="system",
        canonical_sha256=sha256_text(canonical_json(payload)),
        created_at=LATER,
        created_by_kind=SYSTEM.kind,
        created_by_ref=SYSTEM.ref,
        created_by_meta_json=None,
    )
    verify_store.insert_record(store, locked)

    projection = list_effective_verification_configuration(
        store,
        document_id=document.id,
    )
    effective = _criterion(
        projection,
        "terminology_exact_match",
    )["effective_activation"]
    assert effective["id"] == locked.id
    assert effective["required"] is True
    assert effective["locked"] is True
    with pytest.raises(
        VerifyInvariantViolation,
        match="required criterion cannot be disabled",
    ):
        set_document_criterion_enabled(
            store,
            document_id=document.id,
            criterion_key="terminology_exact_match",
            enabled=False,
            actor=HUMAN,
        )


def test_unadmitted_check_is_truthfully_unavailable_and_cannot_be_enabled(
    store_ctx,
):
    store = store_ctx["store"]
    document = _document(store_ctx)
    criterion = CriterionDefinitionVersion(
        id=new_id(),
        stable_key="user_model_draft",
        version=1,
        title="Unadmitted draft",
        description="A user-authored criterion without an admitted executor.",
        criterion_kind="style",
        origin="user",
        configuration_schema_json=canonical_json({"type": "object"}),
        canonical_sha256=sha256_text("user-model-draft-criterion"),
        created_at=NOW,
        created_by_kind=HUMAN.kind,
        created_by_ref=HUMAN.ref,
        created_by_meta_json=None,
    )
    check = CheckDefinitionVersion(
        id=new_id(),
        stable_key="user_model_draft",
        version=1,
        title="Unadmitted model check",
        mechanism="model",
        executor_ref="unadmitted:model-check",
        supported_criterion_kinds_json=canonical_json(["style"]),
        input_schema_json=canonical_json({"type": "object"}),
        output_schema_json=canonical_json({"type": "object"}),
        limitations_json=canonical_json(["No executor has been admitted."]),
        origin="user",
        canonical_sha256=sha256_text("user-model-draft-check"),
        created_at=NOW,
        created_by_kind=HUMAN.kind,
        created_by_ref=HUMAN.ref,
        created_by_meta_json=None,
    )
    binding = CriterionCheckBinding(
        id=new_id(),
        criterion_definition_version_id=criterion.id,
        check_definition_version_id=check.id,
        configuration_json=canonical_json({}),
        canonical_sha256=sha256_text("user-model-draft-binding"),
        created_at=NOW,
        created_by_kind=HUMAN.kind,
        created_by_ref=HUMAN.ref,
        created_by_meta_json=None,
    )
    activation = CriterionActivation(
        id=new_id(),
        criterion_definition_version_id=criterion.id,
        criterion_check_binding_id=binding.id,
        scope_json=canonical_json(
            {"kind": "document", "document_id": document.id}
        ),
        is_enabled=0,
        is_required=0,
        origin="user",
        canonical_sha256=sha256_text("user-model-draft-activation"),
        created_at=NOW,
        created_by_kind=HUMAN.kind,
        created_by_ref=HUMAN.ref,
        created_by_meta_json=None,
    )
    with store.write_transaction() as conn:
        for record in (criterion, check, binding, activation):
            verify_store.insert_record(store, record, conn=conn)

    projection = list_effective_verification_configuration(
        store,
        document_id=document.id,
    )
    draft = _criterion(projection, "user_model_draft")
    assert draft["operational_state"] == "inactive"
    assert draft["mechanism_availability"]["state"] == "unavailable"
    assert draft["checks"][0]["availability"] == {
        "state": "unavailable",
        "reason": "executor_not_admitted",
        "execution_location": None,
    }
    assert draft["checks"][0]["data_sharing"]["class"] == "not_authorized"
    with pytest.raises(
        VerifyInvariantViolation,
        match="no admitted available check",
    ):
        set_document_criterion_enabled(
            store,
            document_id=document.id,
            criterion_key="user_model_draft",
            enabled=True,
            actor=HUMAN,
        )


def test_user_criterion_draft_preserves_authorship_without_admitting_execution(
    store_ctx,
):
    document = _document(store_ctx)

    created = create_user_criterion_draft(
        store_ctx["store"],
        document_id=document.id,
        title="State the positive claim",
        description=(
            "Prefer direct positive descriptions over repeated statements of "
            "what a concept is not."
        ),
        evaluation_instructions=(
            "Identify negative-definition framing and ask whether it can be "
            "replaced by a direct positive account without changing meaning."
        ),
        limitations=["Negation can be substantively necessary."],
        actor=HUMAN,
        at=NOW,
    )

    draft = _criterion(
        created["configuration"],
        created["criterion_key"],
    )
    assert created["status"] == "draft_unadmitted"
    assert draft["author_origin"]["definition_origin"] == "user"
    assert draft["author_origin"]["author"]["kind"] == "human"
    assert draft["operational_state"] == "inactive"
    assert draft["mechanism_availability"]["state"] == "unavailable"
    assert draft["checks"][0]["method"] == {
        "mechanism": "model_judge_draft",
        "executor_ref": "unadmitted:user-authored-checker",
    }
    assert draft["checks"][0]["binding"]["configuration"]["status"] == (
        "draft_unadmitted"
    )
    assert draft["checks"][0]["data_sharing"]["class"] == "not_authorized"

    repeated = create_user_criterion_draft(
        store_ctx["store"],
        document_id=document.id,
        title="State the positive claim",
        description=(
            "Prefer direct positive descriptions over repeated statements of "
            "what a concept is not."
        ),
        evaluation_instructions=(
            "Identify negative-definition framing and ask whether it can be "
            "replaced by a direct positive account without changing meaning."
        ),
        limitations=["Negation can be substantively necessary."],
        actor=HUMAN,
        at=LATER,
    )
    assert repeated["criterion_key"] == created["criterion_key"]
    with store_ctx["store"].connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM criterion_definition_versions "
            "WHERE stable_key = ?",
            (created["criterion_key"],),
        ).fetchone()[0] == 1


def test_user_verification_check_is_runnable_and_identical_submit_converges(
    store_ctx,
):
    document = _document(store_ctx)
    inputs = {
        "title": "State the positive claim",
        "description": "Prefer direct positive descriptions.",
        "evaluation_instructions": (
            "Identify negative-definition framing and assess whether a direct "
            "positive account would preserve the intended meaning."
        ),
        "limitations": ["Substantive contrasts may require negation."],
    }
    legacy = create_user_criterion_draft(
        store_ctx["store"],
        document_id=document.id,
        actor=HUMAN,
        at=NOW,
        **inputs,
    )

    created = create_user_verification_check(
        store_ctx["store"],
        document_id=document.id,
        actor=HUMAN,
        at=NOW,
        **inputs,
    )

    assert created["created"] is True
    assert created["status"] == "active"
    assert created["criterion_key"] != legacy["criterion_key"]
    criterion = _criterion(
        created["configuration"],
        created["criterion_key"],
    )
    assert criterion["author_origin"] == {
        "definition_origin": "user",
        "author": {
            "kind": "human",
            "ref": HUMAN.ref,
            "meta": None,
        },
    }
    assert criterion["operational_state"] == "active"
    assert criterion["effective_activation"]["enabled"] is True
    assert criterion["effective_activation"]["origin"] == "user"
    assert criterion["effective_activation"]["authorized_by"]["kind"] == (
        "human"
    )
    assert criterion["checks"][0]["origin"]["definition_origin"] == "system"
    assert criterion["checks"][0]["availability"]["state"] == "available"
    assert criterion["checks"][0]["availability"]["execution_location"] == (
        "account_backed_agent"
    )
    assert criterion["checks"][0]["data_sharing"] == {
        "class": "account_backed_agent",
        "external_egress": True,
        "basis": "explicit_verify_run_selection",
    }
    assert criterion["checks"][0]["method"] == {
        "mechanism": "model_judge",
        "executor_ref": "builtin:cowork_verify:instruction_model_check:v1",
    }
    assert criterion["checks"][0]["binding"]["selected"] is True
    assert criterion["checks"][0]["binding"]["configuration"] == {
        "evaluation_instructions": inputs["evaluation_instructions"],
        "limitations": inputs["limitations"],
    }

    repeated = create_user_verification_check(
        store_ctx["store"],
        document_id=document.id,
        actor=HUMAN,
        at=LATER,
        **inputs,
    )
    assert repeated["created"] is False
    assert repeated["criterion_key"] == created["criterion_key"]
    with store_ctx["store"].connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM criterion_definition_versions "
            "WHERE stable_key = ?",
            (created["criterion_key"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM criterion_check_bindings "
            "WHERE criterion_definition_version_id = ?",
            (criterion["id"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM criterion_activations "
            "WHERE criterion_definition_version_id = ?",
            (criterion["id"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM check_definition_versions "
            "WHERE stable_key = 'instruction_model_evaluation'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "create_check",
    (create_user_criterion_draft, create_user_verification_check),
)
def test_oversized_user_check_instructions_fail_before_any_verify_write(
    store_ctx,
    create_check,
):
    store = store_ctx["store"]
    document = _document(store_ctx)
    tables = (
        "criterion_definition_versions",
        "check_definition_versions",
        "criterion_check_bindings",
        "criterion_activations",
    )
    with store.connect() as conn:
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }

    with pytest.raises(
        VerifyInvariantViolation,
        match="evaluation instructions exceed the supported "
        rf"{MAX_USER_CHECK_EVALUATION_INSTRUCTIONS_CHARS}-character boundary",
    ):
        create_check(
            store,
            document_id=document.id,
            title="Bound custom checks",
            description="Reject instructions outside the admitted boundary.",
            evaluation_instructions=(
                "x" * (MAX_USER_CHECK_EVALUATION_INSTRUCTIONS_CHARS + 1)
            ),
            actor=HUMAN,
            at=NOW,
        )

    with store.connect() as conn:
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
    assert after == before


def test_non_human_cannot_author_user_document_override(store_ctx):
    document = _document(store_ctx)
    with pytest.raises(
        VerifyInvariantViolation,
        match="require a human actor",
    ):
        set_document_criterion_enabled(
            store_ctx["store"],
            document_id=document.id,
            criterion_key="terminology_exact_match",
            enabled=False,
            actor=SYSTEM,
        )


def test_document_override_rejects_a_stale_effective_activation(store_ctx):
    store = store_ctx["store"]
    document = _document(store_ctx)
    current = list_effective_verification_configuration(
        store,
        document_id=document.id,
    )
    activation_id = _criterion(
        current,
        "terminology_exact_match",
    )["effective_activation"]["id"]

    changed = set_document_criterion_enabled(
        store,
        document_id=document.id,
        criterion_key="terminology_exact_match",
        enabled=False,
        actor=HUMAN,
        expected_activation_id=activation_id,
    )
    assert changed["changed"] is True

    with pytest.raises(
        VerifyInvariantViolation,
        match="configuration changed",
    ):
        set_document_criterion_enabled(
            store,
            document_id=document.id,
            criterion_key="terminology_exact_match",
            enabled=True,
            actor=HUMAN,
            expected_activation_id=activation_id,
        )
