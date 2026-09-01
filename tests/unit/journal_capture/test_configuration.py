from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from flask import Flask
import pytest

from work_buddy.journal_capture import api as journal_api
from work_buddy.journal_capture.configuration import JournalProfileConfigurationService
from work_buddy.journal_capture.content_adapter import JournalContentAdapter
from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.native_source import JournalNativeSourceService
from work_buddy.journal_capture.models import JournalCaptureValidationError
from work_buddy.journal_capture.projection import view_snapshot
from work_buddy.journal_capture.service import JournalCaptureService
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.sources.models import ActorRef
from work_buddy.sources.store import SourceStore


def _draft() -> dict:
    return {
        "profileId": "user.focus",
        "expectedRevision": 0,
        "name": "Focus Journal",
        "description": "A small custom reflection.",
        "modules": [
            {
                "slotId": "focus-reflection",
                "moduleInstanceId": "user.focus.reflection",
                "expectedVersion": 0,
                "moduleTypeId": "field_group",
                "moduleTypeVersion": 1,
                "label": "Focus reflection",
                "behaviorId": "human_value",
                "behaviorVersion": 1,
                "scheduleKind": "weekdays",
                "schedule": {"weekdays": [0, 1, 2, 3, 4]},
                "fields": [
                    {
                        "slotId": "clarity",
                        "fieldId": "user.focus.clarity",
                        "expectedVersion": 0,
                        "owner": "user",
                        "stableKey": "clarity",
                        "label": "Clarity",
                        "description": "Current clarity.",
                        "valueKind": "scale",
                        "constraints": {"minimum": 1, "maximum": 5},
                        "behaviorId": "human_value",
                        "behaviorVersion": 1,
                        "prompt": {
                            "promptId": "user.focus.clarity.prompt",
                            "expectedVersion": 0,
                            "wording": "How clear is the next step?",
                            "helpText": "Use a 1–5 scale.",
                            "requiredness": "optional",
                            "scheduleKind": "always",
                        },
                    }
                ],
            }
        ],
    }


def test_profile_configuration_preview_is_pure_and_save_is_atomic_idempotent(tmp_path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    service = JournalProfileConfigurationService(store)
    catalog = service.catalog()
    simple = next(item for item in catalog["profiles"] if item["profileId"] == "simple-journal")
    assert [item["moduleTypeId"] for item in simple["modules"]] == [
        "capture", "day_stream", "record_collection"
    ]
    assert all(not item["fields"] for item in simple["modules"])

    with store._connect() as conn:
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "journal_profile_revisions",
                "journal_module_instance_versions",
                "journal_field_definition_versions",
                "journal_prompt_definition_versions",
            )
        }
    preview = service.preview(_draft(), local_date="2026-08-27")
    assert preview["modules"][0]["semanticMembership"] == "included"
    assert preview["modules"][0]["fields"][0]["promptWording"] == (
        "How clear is the next step?"
    )
    with store._connect() as conn:
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
    assert after == before

    first = service.save(
        _draft(), client_mutation_id="save-focus", actor={"subject": "person:test"}
    )
    replay = service.save(
        _draft(), client_mutation_id="save-focus", actor={"subject": "person:test"}
    )
    assert replay == first
    configured = next(
        item for item in service.catalog()["profiles"]
        if item["profileId"] == "user.focus"
    )
    assert configured["modules"][0]["schedule"] == {"weekdays": [0, 1, 2, 3, 4]}
    assert configured["modules"][0]["fields"][0]["valueKind"] == "scale"


def test_profile_preview_applies_prompt_schedule_to_the_selected_day(tmp_path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    service = JournalProfileConfigurationService(store)
    draft = _draft()
    draft["modules"][0]["scheduleKind"] = "always"
    draft["modules"][0]["schedule"] = {}
    prompt = draft["modules"][0]["fields"][0]["prompt"]
    prompt["requiredness"] = "required"
    prompt["scheduleKind"] = "weekdays"
    prompt["schedule"] = {"weekdays": [3]}

    included = service.preview(draft, local_date="2026-08-27")
    included_field = included["modules"][0]["fields"][0]
    assert included_field["promptWording"] == "How clear is the next step?"
    assert included_field["requiredness"] == "required"

    excluded = service.preview(draft, local_date="2026-08-28")
    excluded_field = excluded["modules"][0]["fields"][0]
    assert excluded_field["promptWording"] is None
    assert excluded_field["promptHelp"] is None
    assert excluded_field["requiredness"] == "optional"


def test_prompt_result_requires_an_ai_contribution_behavior(tmp_path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    service = JournalProfileConfigurationService(store)
    draft = _draft()
    draft["modules"][0]["moduleTypeId"] = "prompt_result"

    with pytest.raises(
        JournalCaptureValidationError,
        match="requires a behavior that permits AI contribution",
    ):
        service.preview(draft, local_date="2026-08-27")

    draft["modules"][0]["behaviorId"] = "provenance_only"
    preview = service.preview(draft, local_date="2026-08-27")
    assert preview["modules"][0]["moduleTypeId"] == "prompt_result"


def test_document_module_requires_provenance_and_disabled_truth_default(tmp_path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    service = JournalProfileConfigurationService(store)
    draft = _draft()
    module = draft["modules"][0]
    module["moduleTypeId"] = "document"
    module["fields"] = []

    with pytest.raises(JournalCaptureValidationError, match="retain Co-work provenance"):
        service.preview(draft, local_date="2026-08-27")

    module["behaviorId"] = "provenance_only"
    module["settings"] = {"initialTruthActivation": "enabled"}
    with pytest.raises(JournalCaptureValidationError, match="start with Truth disabled"):
        service.preview(draft, local_date="2026-08-27")

    module["settings"] = {"documentRole": "daily_reflection"}
    service.save(
        draft,
        client_mutation_id="save-document-profile",
        actor={"subject": "person:test"},
    )
    configured = next(
        item for item in service.catalog()["profiles"]
        if item["profileId"] == "user.focus"
    )["modules"][0]
    assert configured["settings"] == {
        "documentRole": "daily_reflection",
        "truthEligibility": "allowed",
        "initialTruthActivation": "disabled",
    }


def test_profile_fields_bind_only_to_compatible_registered_functions(tmp_path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    domain = JournalDomainService(store)
    domain.create_function_contract_version(
        function_id="function.focus",
        value_kind="scale",
        unit="points",
        cardinality="single",
        definition={"label": "Focus"},
    )
    service = JournalProfileConfigurationService(store)
    draft = _draft()
    field = draft["modules"][0]["fields"][0]
    field["unit"] = "points"
    field["functionId"] = "function.focus"
    field["functionVersion"] = 1

    catalog_function = next(
        item for item in service.catalog()["functions"]
        if item["functionId"] == "function.focus"
    )
    assert catalog_function == {
        "functionId": "function.focus",
        "functionVersion": 1,
        "valueKind": "scale",
        "unit": "points",
        "cardinality": "single",
        "definition": {"label": "Focus"},
    }
    preview = service.preview(draft, local_date="2026-08-27")
    assert preview["modules"][0]["fields"][0]["functionId"] == "function.focus"

    service.save(
        draft,
        client_mutation_id="save-function-aware-focus",
        actor={"subject": "person:test"},
    )
    configured = next(
        item for item in service.catalog()["profiles"]
        if item["profileId"] == "user.focus"
    )
    assert configured["modules"][0]["fields"][0]["functionId"] == "function.focus"
    assert configured["modules"][0]["fields"][0]["functionVersion"] == 1

    field["unit"] = "minutes"
    with pytest.raises(JournalCaptureValidationError, match="function contract"):
        service.preview(draft, local_date="2026-08-27")


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("privacyClass", "public", "privacy class"),
        ("searchMode", "everything", "search mode"),
    ],
)
def test_profile_configuration_rejects_unknown_field_policy_values(
    tmp_path, field_name, value, message
):
    store = JournalCaptureStore(tmp_path / "journal.db")
    draft = _draft()
    draft["modules"][0]["fields"][0][field_name] = value

    with pytest.raises(JournalCaptureValidationError, match=message):
        JournalProfileConfigurationService(store).preview(
            draft, local_date="2026-08-27"
        )


def test_profile_revision_preserves_user_owned_semantic_identities(tmp_path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    service = JournalProfileConfigurationService(store)
    service.save(
        _draft(), client_mutation_id="save-focus-v1", actor={"subject": "person:test"}
    )
    revision = deepcopy(_draft())
    revision["expectedRevision"] = 1
    module = revision["modules"][0]
    module["expectedVersion"] = 1
    module["label"] = "Revised focus reflection"
    field = module["fields"][0]
    field["expectedVersion"] = 1
    field["description"] = "Clarity after reflection."
    field["prompt"]["expectedVersion"] = 1
    field["prompt"]["wording"] = "How clear is the next action?"

    result = service.save(
        revision,
        client_mutation_id="save-focus-v2",
        actor={"subject": "person:test"},
    )

    assert result["profileId"] == "user.focus"
    assert result["profileRevision"] == 2
    latest = next(
        item
        for item in service.catalog()["profiles"]
        if item["profileId"] == "user.focus"
    )
    latest_module = latest["modules"][0]
    latest_field = latest_module["fields"][0]
    assert latest["editable"] is True
    assert latest["supersedesRevision"] == 1
    assert (latest_module["slotId"], latest_module["moduleInstanceId"]) == (
        "focus-reflection",
        "user.focus.reflection",
    )
    assert latest_module["moduleInstanceVersion"] == 2
    assert (
        latest_field["slotId"],
        latest_field["fieldId"],
        latest_field["stableKey"],
    ) == ("clarity", "user.focus.clarity", "clarity")
    assert latest_field["fieldDefinitionVersion"] == 2
    assert latest_field["prompt"]["promptId"] == "user.focus.clarity.prompt"
    assert latest_field["prompt"]["promptVersion"] == 2
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_profile_revisions WHERE profile_id=?",
            ("user.focus",),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_field_definition_versions WHERE field_id=?",
            ("user.focus.clarity",),
        ).fetchone()[0] == 2


@pytest.mark.parametrize("identity_kind", ["profile", "module", "field", "prompt"])
def test_profile_save_rejects_reserved_catalog_identities(tmp_path, identity_kind):
    store = JournalCaptureStore(tmp_path / "journal.db")
    service = JournalProfileConfigurationService(store)
    draft = _draft()
    if identity_kind == "profile":
        draft["profileId"] = "simple-journal"
        draft["expectedRevision"] = 1
    elif identity_kind == "module":
        draft["modules"][0]["moduleInstanceId"] = "simple.capture"
    elif identity_kind == "field":
        draft["modules"][0]["fields"][0]["fieldId"] = "simple.focus"
    else:
        draft["modules"][0]["fields"][0]["prompt"]["promptId"] = (
            "simple.focus.prompt"
        )

    with pytest.raises(JournalCaptureValidationError, match="reserved"):
        service.save(
            draft,
            client_mutation_id=f"reserved-{identity_kind}",
            actor={"subject": "person:test"},
        )

    simple = next(
        item
        for item in service.catalog()["profiles"]
        if item["profileId"] == "simple-journal"
    )
    assert simple["profileRevision"] == 1
    assert simple["editable"] is False


@pytest.mark.parametrize(
    ("field_change", "message"),
    [
        ({"owner": "work-buddy"}, "remain owned by the user"),
        ({"stableKey": "renamed-clarity"}, "key cannot change"),
    ],
)
def test_profile_revision_rejects_field_ownership_or_stable_key_changes(
    tmp_path, field_change, message
):
    store = JournalCaptureStore(tmp_path / "journal.db")
    service = JournalProfileConfigurationService(store)
    service.save(
        _draft(), client_mutation_id="save-owned-field", actor={"subject": "person:test"}
    )
    revision = deepcopy(_draft())
    revision["expectedRevision"] = 1
    revision["modules"][0]["expectedVersion"] = 1
    field = revision["modules"][0]["fields"][0]
    field["expectedVersion"] = 1
    field["prompt"]["expectedVersion"] = 1
    field.update(field_change)

    with pytest.raises(JournalCaptureValidationError, match=message):
        service.save(
            revision,
            client_mutation_id=f"reject-field-{next(iter(field_change))}",
            actor={"subject": "person:test"},
        )

    with store._connect() as conn:
        assert conn.execute(
            "SELECT MAX(profile_revision) FROM journal_profile_revisions WHERE profile_id=?",
            ("user.focus",),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_field_definition_versions WHERE field_id=?",
            ("user.focus.clarity",),
        ).fetchone()[0] == 1


def test_configuration_http_save_and_future_activation_require_human_gestures(
    tmp_path, monkeypatch
):
    store = JournalCaptureStore(tmp_path / "journal.db")
    sources = SourceStore.create(tmp_path / "sources")
    vault = tmp_path / "vault"
    vault.mkdir()
    capture_service = JournalCaptureService(store, JournalContentAdapter(vault))
    monkeypatch.setattr(journal_api, "_runtime", (sources, store, capture_service))
    monkeypatch.setattr(journal_api, "_recovery_complete", True)
    actor = ActorRef(
        issuer_authority_id="authority-test-0001",
        subject="person-test-0001",
        kind="human",
        tenant_scope_id="tenant-test-0001",
    )
    gestures: list[tuple[str, str]] = []

    def authorize(*, action, subject, context_sha256):
        gestures.append((action, subject))
        assert len(context_sha256) == 64
        return SimpleNamespace(principal=SimpleNamespace(actor=actor))

    monkeypatch.setattr(journal_api, "require_human_authority_request", authorize)
    app = Flask("journal-configuration")
    journal_api.register_routes(app)
    client = app.test_client()

    saved = client.post(
        "/api/journal/configuration/profiles",
        json={"clientMutationId": "http-save-focus", "draft": _draft()},
    )
    assert saved.status_code == 201, saved.json
    activated = client.post(
        "/api/journal/configuration/profiles/user.focus/1/activate",
        json={
            "clientMutationId": "http-activate-focus",
            "expectedActivationRevision": 1,
            "effectiveLocalDate": "2099-01-01",
        },
    )
    assert activated.status_code == 200, activated.json
    assert activated.json["activation"]["activationRevision"] == 2
    assert gestures == [
        ("journal.profile.save", "journal-profile:user.focus"),
        ("journal.profile.activate", "journal-profile:user.focus:1"),
    ]


def test_projection_exposes_module_owned_generated_artifacts(tmp_path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    domain = JournalDomainService(store)
    domain.create_native_item(
        local_date="2026-08-27",
        item_kind="generated_artifact",
        plain_value="A generated daily briefing.",
        source_ref="wb-source://journal/generated-1",
        interaction_behavior_id="human_value",
        interaction_behavior_version=1,
        client_mutation_id="generated-1",
        actor={"subject": "service:test"},
        module_instance_id="simple.notes",
        module_instance_version=1,
        authorship="ai",
    )
    view = view_snapshot(store, local_date="2026-08-27")
    assert view["nativeItems"] == [
        {
            "itemId": view["nativeItems"][0]["itemId"],
            "itemKind": "generated_artifact",
            "text": "A generated daily briefing.",
            "createdAt": view["nativeItems"][0]["createdAt"],
            "updatedAt": view["nativeItems"][0]["updatedAt"],
            "revision": 1,
            "lifecycle": "current",
            "authorityKind": "native_plain",
            "sourceRef": "wb-source://journal/generated-1",
            "moduleInstanceId": "simple.notes",
            "moduleInstanceVersion": 1,
            "actions": ["edit", "correct", "resolve", "route", "tombstone"],
            "relations": [],
        }
    ]


def test_http_field_edit_is_human_gesture_bound_source_backed_and_replay_safe(
    tmp_path, monkeypatch
):
    store = JournalCaptureStore(tmp_path / "journal.db")
    sources = SourceStore.create(tmp_path / "sources")
    vault = tmp_path / "vault"
    vault.mkdir()
    capture_service = JournalCaptureService(store, JournalContentAdapter(vault))
    configuration = JournalProfileConfigurationService(store)
    draft = _draft()
    draft["modules"][0]["scheduleKind"] = "always"
    draft["modules"][0]["schedule"] = {}
    configuration.save(
        draft,
        client_mutation_id="save-field-http-profile",
        actor={"subject": "person:test"},
    )
    local_date = journal_api.current_day()["localDate"]
    domain = JournalDomainService(store)
    domain.activate_profile(
        profile_id="user.focus",
        profile_revision=1,
        effective_local_date=local_date,
        expected_activation_revision=1,
        client_mutation_id="activate-field-http-profile",
        actor={"subject": "person:test"},
    )
    with store.transaction() as conn:
        conn.execute(
            "UPDATE journal_authority_control SET mode='database_only' WHERE singleton=1"
        )
        conn.execute(
            "UPDATE journal_domain_state SET value='database_only' "
            "WHERE key='content_authority'"
        )
    monkeypatch.setattr(journal_api, "_runtime", (sources, store, capture_service))
    monkeypatch.setattr(journal_api, "_recovery_complete", True)
    actor = ActorRef(
        issuer_authority_id="authority-test-0001",
        subject="person-test-0001",
        kind="human",
        tenant_scope_id="tenant-test-0001",
    )
    gestures: list[tuple[str, str]] = []

    def authorize(*, action, subject, context_sha256):
        gestures.append((action, subject))
        assert len(context_sha256) == 64
        return SimpleNamespace(
            principal=SimpleNamespace(actor=actor),
            gesture_id=f"gesture-{len(gestures)}",
            action=action,
            assurance="enrolled_local_session_gesture",
        )

    monkeypatch.setattr(journal_api, "require_human_authority_request", authorize)
    calls: list[dict] = []
    original_put = JournalNativeSourceService.put_field_value

    def source_backed_put(self, **kwargs):
        calls.append(kwargs)
        return original_put(self, **kwargs)

    monkeypatch.setattr(JournalNativeSourceService, "put_field_value", source_backed_put)
    app = Flask("journal-field-value")
    journal_api.register_routes(app)
    client = app.test_client()
    body = {
        "clientMutationId": "journal-field-edit-0001",
        "localDate": local_date,
        "moduleInstanceId": "user.focus.reflection",
        "moduleInstanceVersion": 1,
        "compositionSlotId": "focus-reflection:clarity",
        "fieldId": "user.focus.clarity",
        "fieldDefinitionVersion": 1,
        "expectedRevision": 0,
        "exactInput": "4",
        "value": 4,
        "disposition": None,
    }
    first = client.post("/api/journal/field-values", json=body)
    replay = client.post("/api/journal/field-values", json=body)
    stale = client.post(
        "/api/journal/field-values",
        json={
            **body,
            "clientMutationId": "journal-field-edit-stale",
            "exactInput": "5",
            "value": 5,
        },
    )

    assert first.status_code == replay.status_code == 200, first.json
    assert stale.status_code == 409
    assert "changed" in stale.json["error"]["message"]
    assert first.json["fieldValue"] == replay.json["fieldValue"]
    assert first.json["fieldValue"]["value"] == 4.0
    assert first.json["fieldValue"]["authorship"] == "human"
    assert first.json["fieldValue"]["currentRevision"] == 1
    assert gestures == [
        (
            "journal.field_value.put",
            f"journal-field:{local_date}:user.focus.reflection:user.focus.clarity",
        ),
        (
            "journal.field_value.put",
            f"journal-field:{local_date}:user.focus.reflection:user.focus.clarity",
        ),
        (
            "journal.field_value.put",
            f"journal-field:{local_date}:user.focus.reflection:user.focus.clarity",
        ),
    ]
    assert len(calls) == 2
    assert all(call["source_ref"].uri == first.json["fieldValue"]["sourceRef"] for call in calls)
    assert all(call["expected_revision"] == 0 for call in calls)
    with store._connect() as conn:
        dependency = conn.execute(
            "SELECT state,value_revision FROM journal_field_source_dependencies"
        ).fetchone()
        assert tuple(dependency) == ("acknowledged", 1)
        assert conn.execute("SELECT COUNT(*) FROM journal_field_values").fetchone()[0] == 1
    with sources.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
        assert tuple(
            conn.execute(
                "SELECT status,purpose,use_kind FROM source_usage_intents"
            ).fetchone()
        ) == ("acknowledged", "journal.field_value", "journal_field_value")
