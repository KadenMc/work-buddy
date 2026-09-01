from __future__ import annotations

from work_buddy.journal_capture import native_ops
from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.security.local_identity import LocalIdentityAuthority
from work_buddy.sources import SourceStore


def _day(local_date: str = "2026-08-27") -> dict[str, str]:
    return {
        "dayId": f"journal-day:{local_date}:America/New_York:04:00",
        "localDate": local_date,
        "timezone": "America/New_York",
        "dayBoundaryStart": "04:00",
        "windowStart": f"{local_date}T04:00:00-04:00",
        "windowEnd": f"{local_date}T23:59:59-04:00",
        "now": "2026-08-27T18:00:00+00:00",
    }


def _install_native_runtime(monkeypatch, store: JournalCaptureStore) -> None:
    monkeypatch.setattr(native_ops, "_read_runtime", lambda: (store, object()))
    monkeypatch.setattr(native_ops, "_write_runtime", lambda: (object(), store, object()))
    monkeypatch.setattr(native_ops, "_authority_mode", lambda _store: "database_only")
    monkeypatch.setattr(
        native_ops,
        "current_day",
        lambda local_date=None: _day(local_date or "2026-08-27"),
    )


def _item(
    store: JournalCaptureStore,
    *,
    local_date: str,
    item_kind: str,
    value: str,
    mutation: str,
) -> None:
    domain = JournalDomainService(store)
    composition = native_ops._composition(domain, local_date, persist=True)
    module = next(
        item.module
        for item in composition.modules
        if item.module.module_instance_id
        == ("simple.notes" if item_kind == "running_note" else "simple.stream")
    )
    domain.create_native_item(
        local_date=local_date,
        item_kind=item_kind,
        plain_value=value,
        source_ref=f"wb-source://authority/source-{mutation}",
        interaction_behavior_id=module.behavior_id or "human_value",
        interaction_behavior_version=module.behavior_version or 1,
        client_mutation_id=mutation,
        actor={"kind": "test"},
        module_instance_id=module.module_instance_id,
        module_instance_version=module.instance_version,
    )


def _activate_field_profile(store: JournalCaptureStore) -> None:
    domain = JournalDomainService(store)
    domain.create_field_definition_version(
        field_id="rating",
        owner="test",
        stable_key="rating",
        label="Rating",
        value_kind="scale",
        constraints={"minimum": 1, "maximum": 5},
        search_mode="structured_only",
    )
    domain.create_module_instance_version(
        module_instance_id="reflection",
        module_type_id="field_group",
        module_type_version=1,
        label="Reflection",
        fields=[
            {
                "slot_id": "rating",
                "field_id": "rating",
                "field_definition_version": 1,
            }
        ],
    )
    domain.create_profile_revision(
        profile_id="custom",
        name="Custom",
        modules=[
            {
                "slot_id": "reflection",
                "module_instance_id": "reflection",
                "module_instance_version": 1,
            }
        ],
        created_by="test",
    )
    domain.activate_profile(
        profile_id="custom",
        profile_revision=1,
        effective_local_date="2026-08-27",
        expected_activation_revision=1,
        client_mutation_id="activate-native-ops-custom",
        actor={"kind": "human", "subject": "test"},
    )


def test_journal_state_reads_native_items_without_markdown(tmp_path, monkeypatch) -> None:
    store = JournalCaptureStore(tmp_path / "journal.db")
    _install_native_runtime(monkeypatch, store)
    _item(
        store,
        local_date="2026-08-27",
        item_kind="record",
        value="native log",
        mutation="mutation-native-log",
    )

    result = native_ops.journal_state(target="today")

    assert result["authority_state"] == "database_only"
    assert result["target_date"] == "2026-08-27"
    assert result["log_section"] == "native log"
    assert result["items"][0]["value"] == "native log"


def test_running_notes_filters_native_days(tmp_path, monkeypatch) -> None:
    store = JournalCaptureStore(tmp_path / "journal.db")
    _install_native_runtime(monkeypatch, store)
    _item(
        store,
        local_date="2026-08-27",
        item_kind="running_note",
        value="today note",
        mutation="mutation-note-today",
    )
    _item(
        store,
        local_date="2026-08-26",
        item_kind="running_note",
        value="yesterday note",
        mutation="mutation-note-yesterday",
    )

    assert native_ops.running_notes(same_day=True) == "today note"
    combined = native_ops.running_notes(days=2)
    assert "## 2026-08-27\n\ntoday note" in combined
    assert "## 2026-08-26\n\nyesterday note" in combined


def test_create_on_read_persists_only_the_native_day(tmp_path, monkeypatch) -> None:
    store = JournalCaptureStore(tmp_path / "journal.db")
    _install_native_runtime(monkeypatch, store)

    result = native_ops.journal_state(target="today", create_on_read=True)

    assert result["created"] is True
    assert result["exists"] is True
    assert native_ops._day_exists(store, "2026-08-27")


def test_journal_write_records_agent_source_and_unreviewed_provenance(
    tmp_path,
    monkeypatch,
) -> None:
    store = JournalCaptureStore(tmp_path / "journal.db")
    sources = SourceStore.create(tmp_path / "sources")
    identity = LocalIdentityAuthority(tmp_path / "identity.db")
    _install_native_runtime(monkeypatch, store)
    monkeypatch.setattr(
        native_ops,
        "_write_runtime",
        lambda: (sources, store, object()),
    )
    monkeypatch.setattr(
        "work_buddy.dashboard.local_identity_api._authority",
        lambda: identity,
    )
    monkeypatch.setattr(
        "work_buddy.agent_session.get_originating_session",
        lambda: "session-native-journal-write",
    )

    result = native_ops.journal_write(
        entries='[["3:00 PM", "Agent-authored update"]]',
        client_mutation_id="journal-write-test-0001",
    )
    retry = native_ops.journal_write(
        entries='[["3:00 PM", "Agent-authored update"]]',
        client_mutation_id="journal-write-test-0001",
    )

    assert result["success"] is True
    assert result["entries_written"] == 1
    assert retry["items"] == result["items"]
    with store._connect() as conn:
        item = conn.execute(
            "SELECT item_kind,module_instance_id,current_plain_value "
            "FROM journal_items"
        ).fetchone()
        revision = conn.execute(
            "SELECT authorship,review_state FROM journal_item_revisions"
        ).fetchone()
    assert tuple(item) == (
        "record",
        "simple.stream",
        "3:00 PM - Agent-authored update",
    )
    assert tuple(revision) == ("ai", "unreviewed")
    with sources.connect() as conn:
        source = conn.execute(
            "SELECT source_role FROM source_items"
        ).fetchone()
        author = conn.execute(
            "SELECT actor_ref_json FROM source_attributions WHERE role='author'"
        ).fetchone()
        usage = conn.execute(
            "SELECT purpose,status FROM source_usage_intents"
        ).fetchone()
    assert source["source_role"] == "agent_output"
    assert '"kind":"agent_run"' in author["actor_ref_json"]
    assert tuple(usage) == ("journal.native_item", "acknowledged")


def test_journal_sign_in_writes_profile_field_through_human_source(
    tmp_path,
    monkeypatch,
) -> None:
    store = JournalCaptureStore(tmp_path / "journal.db")
    sources = SourceStore.create(tmp_path / "sources")
    identity = LocalIdentityAuthority(tmp_path / "identity.db")
    _activate_field_profile(store)
    _install_native_runtime(monkeypatch, store)
    monkeypatch.setattr(
        native_ops,
        "_write_runtime",
        lambda: (sources, store, object()),
    )
    monkeypatch.setattr(
        "work_buddy.dashboard.local_identity_api._authority",
        lambda: identity,
    )
    monkeypatch.setattr(
        "work_buddy.agent_session.get_originating_session",
        lambda: "session-native-journal-field",
    )

    result = native_ops.journal_sign_in(
        write_fields={"rating": {"value": 4, "expected_revision": 0}},
        client_mutation_id="journal-field-test-0001",
    )
    retry = native_ops.journal_sign_in(
        write_fields={"rating": {"value": 4, "expected_revision": 0}},
        client_mutation_id="journal-field-test-0001",
    )

    assert result["write_result"]["fields_written"] == 1
    assert retry["write_result"]["fields"] == result["write_result"]["fields"]
    field = result["sign_in"]["fields"][0]
    assert field["field_id"] == "rating"
    assert field["value"] == 4.0
    assert field["authorship"] == "human"
    assert field["review_state"] == "not_applicable"
    with store._connect() as conn:
        current = conn.execute(
            "SELECT authorship,review_state FROM journal_field_values"
        ).fetchone()
        revision = conn.execute(
            "SELECT authorship,review_state FROM journal_field_value_revisions"
        ).fetchone()
    assert tuple(current) == ("human", "not_applicable")
    assert tuple(revision) == ("human", "not_applicable")
    with sources.connect() as conn:
        source = conn.execute("SELECT source_role FROM source_items").fetchone()
        usage = conn.execute(
            "SELECT purpose,status FROM source_usage_intents"
        ).fetchone()
    assert source["source_role"] == "human_input"
    assert tuple(usage) == ("journal.field_value", "acknowledged")


def test_day_planner_generates_persists_and_reads_native_artifact(
    tmp_path,
    monkeypatch,
) -> None:
    store = JournalCaptureStore(tmp_path / "journal.db")
    sources = SourceStore.create(tmp_path / "sources")
    identity = LocalIdentityAuthority(tmp_path / "identity.db")
    _install_native_runtime(monkeypatch, store)
    monkeypatch.setattr(
        native_ops,
        "_write_runtime",
        lambda: (sources, store, object()),
    )
    monkeypatch.setattr(
        "work_buddy.dashboard.local_identity_api._authority",
        lambda: identity,
    )
    monkeypatch.setattr(
        "work_buddy.agent_session.get_originating_session",
        lambda: "session-native-day-plan",
    )

    status = native_ops.day_planner(action="status")
    result = native_ops.day_planner(
        action="generate_and_write",
        calendar_events=[{"start": "10:00", "end": "10:30", "summary": "Call"}],
        focused_tasks=[{"description": "Draft", "duration": 30}],
        config_overrides={"work_hours": [9, 12], "clamp_to_now": False},
        client_mutation_id="native-day-plan-test-0001",
    )
    reread = native_ops.day_planner(action="read")

    assert status["ready"] is True
    assert status["provider"] == "native"
    assert result["write_result"]["success"] is True
    assert reread["entry_count"] == result["entry_count"]
    assert reread["entries"] == result["entries"]
    assert reread["authority"] == "journal_sqlite"
