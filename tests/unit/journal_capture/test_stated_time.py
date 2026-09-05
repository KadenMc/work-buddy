"""A stated occurrence time is bounded by its Journal day and durable on edit."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from flask import Flask

from work_buddy.dashboard import local_identity_api
from work_buddy.journal_capture import api as journal_api
from work_buddy.journal_capture.content_adapter import JournalContentAdapter
from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.models import (
    JournalCaptureConflict,
    JournalCaptureValidationError,
)
from work_buddy.journal_capture.projection import view_snapshot
from work_buddy.journal_capture.service import JournalCaptureService
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.security.local_identity import (
    DEFAULT_AUDIENCE,
    SESSION_COOKIE_NAME,
    BoundaryRequest,
    LocalIdentityAuthority,
)
from work_buddy.settings import get_journal_day_window
from work_buddy.sources.models import ActorRef
from work_buddy.sources.store import SourceStore


LOCAL_DATE = "2026-08-27"
ORIGIN = "http://127.0.0.1:5127"


def _boundary() -> BoundaryRequest:
    return BoundaryRequest(
        remote_addr="127.0.0.1",
        scheme="http",
        host="127.0.0.1:5127",
        origin=ORIGIN,
    )


def _session(authority: LocalIdentityAuthority):
    bootstrap = authority.mint_bootstrap(origin=ORIGIN, audience=DEFAULT_AUDIENCE)
    return authority.redeem_bootstrap(
        token=bootstrap.token,
        boundary=_boundary(),
        audience=DEFAULT_AUDIENCE,
    )


def _day_id(local_date: str = LOCAL_DATE) -> str:
    window = get_journal_day_window(local_date)
    return f"journal-day:{local_date}:{window.timezone}:{window.boundary}"


def _write(_rel, abs_path, content, **_kw):
    abs_path.write_bytes(content.encode("utf-8"))
    return True


def _domain(tmp_path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    return store, JournalDomainService(store)


def _record(domain: JournalDomainService, name: str):
    return domain.create_native_item(
        local_date=LOCAL_DATE,
        item_kind="record",
        plain_value=f"{name} as first written",
        source_ref=f"wb-source://test/{name}",
        interaction_behavior_id="human_value",
        interaction_behavior_version=1,
        client_mutation_id=f"create-{name}",
        actor={"subject": "person-test"},
    )


def _database_authority(store: JournalCaptureStore) -> None:
    with store.transaction() as conn:
        conn.execute(
            "UPDATE journal_authority_control SET mode='database_only' "
            "WHERE singleton=1"
        )
        conn.execute(
            "UPDATE journal_domain_state SET value='database_only' "
            "WHERE key='content_authority'"
        )


# ----------------------------------------------------------------------
# A corrected time is durable, ordered, and projected


def test_corrected_stated_time_reorders_the_day_and_reaches_the_projection(tmp_path):
    store, domain = _domain(tmp_path)
    morning = _record(domain, "morning")
    midday = _record(domain, "midday")
    evening = _record(domain, "evening")

    for name, item, stated in (
        ("evening", evening, f"{LOCAL_DATE}T21:30:00-04:00"),
        ("midday", midday, f"{LOCAL_DATE}T12:45:00-04:00"),
        ("morning", morning, f"{LOCAL_DATE}T07:05:00-04:00"),
    ):
        domain.update_native_item(
            item_id=item.item_id,
            expected_revision=1,
            plain_value=f"{name} as corrected",
            client_mutation_id=f"correct-{name}",
            actor={"subject": "person-test"},
            operation="correct",
            stated_at=stated,
        )

    entries = view_snapshot(store, local_date=LOCAL_DATE)["logEntries"]
    assert [entry["itemId"] for entry in entries] == [
        morning.item_id,
        midday.item_id,
        evening.item_id,
    ]
    assert [entry["createdAt"] for entry in entries] == [
        f"{LOCAL_DATE}T07:05:00-04:00",
        f"{LOCAL_DATE}T12:45:00-04:00",
        f"{LOCAL_DATE}T21:30:00-04:00",
    ]
    assert entries[0]["revision"] == 2
    assert entries[0]["text"] == "morning as corrected"


def test_edit_without_a_stated_time_keeps_the_occurrence_time_it_had(tmp_path):
    store, domain = _domain(tmp_path)
    item = _record(domain, "solo")
    domain.update_native_item(
        item_id=item.item_id,
        expected_revision=1,
        plain_value="solo at its stated time",
        client_mutation_id="correct-solo-time",
        actor={"subject": "person-test"},
        operation="correct",
        stated_at=f"{LOCAL_DATE}T09:00:00-04:00",
    )
    domain.update_native_item(
        item_id=item.item_id,
        expected_revision=2,
        plain_value="solo with only its wording changed",
        client_mutation_id="edit-solo-text",
        actor={"subject": "person-test"},
        operation="edit",
    )

    entries = view_snapshot(store, local_date=LOCAL_DATE)["logEntries"]
    assert entries[0]["createdAt"] == f"{LOCAL_DATE}T09:00:00-04:00"
    assert entries[0]["text"] == "solo with only its wording changed"
    assert entries[0]["revision"] == 3


def test_replaying_a_correction_is_idempotent_and_diverging_it_conflicts(tmp_path):
    store, domain = _domain(tmp_path)
    item = _record(domain, "replayed")
    first = domain.update_native_item(
        item_id=item.item_id,
        expected_revision=1,
        plain_value="replayed as corrected",
        client_mutation_id="correct-replayed",
        actor={"subject": "person-test"},
        operation="correct",
        stated_at=f"{LOCAL_DATE}T11:00:00-04:00",
    )
    replay = domain.update_native_item(
        item_id=item.item_id,
        expected_revision=1,
        plain_value="replayed as corrected",
        client_mutation_id="correct-replayed",
        actor={"subject": "person-test"},
        operation="correct",
        stated_at=f"{LOCAL_DATE}T11:00:00-04:00",
    )

    assert replay.current_revision == first.current_revision == 2
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_item_revisions WHERE item_id=?",
            (item.item_id,),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT created_at FROM journal_items WHERE item_id=?",
            (item.item_id,),
        ).fetchone()[0] == f"{LOCAL_DATE}T11:00:00-04:00"

    # The same key carrying a different time is a divergence, not a replay,
    # and it is refused before it can quietly move the entry.
    with pytest.raises(JournalCaptureConflict, match="another request"):
        domain.update_native_item(
            item_id=item.item_id,
            expected_revision=1,
            plain_value="replayed as corrected",
            client_mutation_id="correct-replayed",
            actor={"subject": "person-test"},
            operation="correct",
            stated_at=f"{LOCAL_DATE}T18:00:00-04:00",
        )
    with pytest.raises(JournalCaptureConflict, match="another request"):
        domain.update_native_item(
            item_id=item.item_id,
            expected_revision=1,
            plain_value="replayed as corrected",
            client_mutation_id="correct-replayed",
            actor={"subject": "person-test"},
            operation="correct",
        )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT created_at FROM journal_items WHERE item_id=?",
            (item.item_id,),
        ).fetchone()[0] == f"{LOCAL_DATE}T11:00:00-04:00"


def test_a_stale_expected_revision_still_conflicts_when_a_time_is_corrected(tmp_path):
    _store, domain = _domain(tmp_path)
    item = _record(domain, "stale")
    domain.update_native_item(
        item_id=item.item_id,
        expected_revision=1,
        plain_value="stale as corrected",
        client_mutation_id="correct-stale-first",
        actor={"subject": "person-test"},
        operation="correct",
        stated_at=f"{LOCAL_DATE}T10:00:00-04:00",
    )
    with pytest.raises(JournalCaptureConflict):
        domain.update_native_item(
            item_id=item.item_id,
            expected_revision=1,
            plain_value="stale from a second window",
            client_mutation_id="correct-stale-second",
            actor={"subject": "person-test"},
            operation="correct",
            stated_at=f"{LOCAL_DATE}T10:30:00-04:00",
        )


def test_a_stated_time_without_an_offset_is_refused_by_the_domain(tmp_path):
    _store, domain = _domain(tmp_path)
    item = _record(domain, "naive")
    with pytest.raises(JournalCaptureValidationError, match="time zone offset"):
        domain.update_native_item(
            item_id=item.item_id,
            expected_revision=1,
            plain_value="naive as corrected",
            client_mutation_id="correct-naive",
            actor={"subject": "person-test"},
            operation="correct",
            stated_at=f"{LOCAL_DATE}T10:00:00",
        )


# ----------------------------------------------------------------------
# The capture route bounds a stated time by the day it names


def _capture_client(tmp_path, monkeypatch):
    authority = LocalIdentityAuthority(tmp_path / "identity.db")
    monkeypatch.setattr(local_identity_api, "_authority", lambda: authority)
    sources = SourceStore.create(tmp_path / "sources")
    store = JournalCaptureStore(tmp_path / "journal.db")
    vault = tmp_path / "vault"
    (vault / "journal").mkdir(parents=True)
    (vault / "journal" / f"{LOCAL_DATE}.md").write_text(
        "# **Log**\n\n# **Running Notes / Considerations**\n\n% RUNNING END\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("work_buddy.obsidian.vault_writer.vault_write", _write)
    service = JournalCaptureService(store, JournalContentAdapter(vault))
    monkeypatch.setattr(journal_api, "_runtime", (sources, store, service))
    app = Flask("journal-stated-time")
    journal_api.register_routes(app)
    client = app.test_client()
    session = _session(authority)
    client.set_cookie(SESSION_COOKIE_NAME, session.cookie_token, domain="127.0.0.1")
    return authority, session, client, sources


def _submit(authority, session, client, *, mutation_id: str, stated_at: str):
    body = {
        "client_mutation_id": mutation_id,
        "day_id": _day_id(),
        "target_id": "log",
        "mode": "dumb",
        "exact_text": "A retrospective note",
        "input_mode": "direct_entry",
        "stated_at": stated_at,
    }
    _, gesture = authority.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action="journal.capture.submit",
        subject=f"journal-capture:{mutation_id}",
        context_sha256=journal_api._canonical_gesture_context(body),
    )
    return client.post(
        "/api/journal/captures",
        json=body,
        headers={
            "Origin": ORIGIN,
            "Host": "127.0.0.1:5127",
            "X-WB-CSRF": session.csrf_token,
            "X-WB-Gesture": gesture.token,
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )


@pytest.mark.parametrize(
    ("label", "stated_at"),
    [
        ("naive", f"{LOCAL_DATE}T15:15:00"),
        ("before-window", f"{LOCAL_DATE}T02:00:00-04:00"),
        ("after-window", "2026-08-28T09:00:00-04:00"),
    ],
)
def test_capture_refuses_a_stated_time_that_does_not_belong_to_its_day(
    tmp_path, monkeypatch, label, stated_at
):
    authority, session, client, sources = _capture_client(tmp_path, monkeypatch)
    response = _submit(
        authority,
        session,
        client,
        mutation_id=f"capture-stated-{label}",
        stated_at=stated_at,
    )

    assert response.status_code == 400, response.json
    assert response.json["error"]["code"] == "journal_capture_invalid"
    assert "stated time" in response.json["error"]["message"].lower()
    with sources.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 0


def test_capture_accepts_a_stated_time_inside_the_day_it_names(tmp_path, monkeypatch):
    authority, session, client, _sources = _capture_client(tmp_path, monkeypatch)
    response = _submit(
        authority,
        session,
        client,
        mutation_id="capture-stated-in-window",
        stated_at=f"{LOCAL_DATE}T15:15:00-04:00",
    )

    assert response.status_code == 201, response.json
    assert response.json["capture"]["submittedAt"] is not None


# ----------------------------------------------------------------------
# The item edit route bounds a stated time by the entry's own day


def _item_client(tmp_path, monkeypatch):
    store = JournalCaptureStore(tmp_path / "journal.db")
    sources = SourceStore.create(tmp_path / "sources")
    service = JournalCaptureService(store, JournalContentAdapter(tmp_path / "vault"))
    domain = JournalDomainService(store)
    _database_authority(store)
    item = _record(domain, "edited")
    monkeypatch.setattr(journal_api, "_runtime", (sources, store, service))
    monkeypatch.setattr(journal_api, "_recovery_complete", True)
    human = ActorRef("authority-http", "person-http", "human", "tenant-http")

    def authorize(*, action, subject, context_sha256):
        assert len(context_sha256) == 64
        return SimpleNamespace(
            principal=SimpleNamespace(actor=human),
            gesture_id="gesture-stated-time",
            action=action,
            assurance="enrolled_local_session_gesture",
        )

    monkeypatch.setattr(journal_api, "require_human_authority_request", authorize)
    app = Flask("journal-stated-time-items")
    journal_api.register_routes(app)
    return item, app.test_client(), sources, domain


@pytest.mark.parametrize(
    ("label", "stated_at"),
    [
        ("naive", f"{LOCAL_DATE}T15:15:00"),
        ("before-window", f"{LOCAL_DATE}T03:00:00-04:00"),
        ("after-window", "2026-08-29T09:00:00-04:00"),
    ],
)
def test_item_edit_refuses_a_stated_time_outside_the_entry_day(
    tmp_path, monkeypatch, label, stated_at
):
    item, client, sources, _domain = _item_client(tmp_path, monkeypatch)
    response = client.post(
        f"/api/journal/items/{item.item_id}/correct",
        json={
            "clientMutationId": f"http-correct-{label}",
            "expectedRevision": 1,
            "exactText": "Edited with an impossible time",
            "statedAt": stated_at,
        },
    )

    assert response.status_code == 400, response.json
    assert response.json["error"]["code"] == "journal_capture_invalid"
    assert "stated time" in response.json["error"]["message"].lower()
    with sources.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 0


def test_item_edit_accepts_a_stated_time_inside_the_entry_day(tmp_path, monkeypatch):
    item, client, _sources, domain = _item_client(tmp_path, monkeypatch)
    before = domain.get_native_item(item.item_id).created_at
    response = client.post(
        f"/api/journal/items/{item.item_id}/correct",
        json={
            "clientMutationId": "http-correct-in-window",
            "expectedRevision": 1,
            "exactText": "Edited with a time it could have happened",
            "statedAt": f"{LOCAL_DATE}T15:15:00-04:00",
        },
    )

    assert response.status_code == 200, response.json
    assert response.json["item"]["revision"] == 2
    # The route has to carry the corrected time the whole way down, not just
    # accept it. Asserting the revision alone would pass while the entry kept
    # the occurrence time it was written with.
    after = domain.get_native_item(item.item_id).created_at
    assert after != before
    assert after == f"{LOCAL_DATE}T15:15:00-04:00"


# ----------------------------------------------------------------------
# A Journal day that has not started is not reachable


def _view_client(tmp_path, monkeypatch):
    store = JournalCaptureStore(tmp_path / "journal.db")
    sources = SourceStore.create(tmp_path / "sources")
    vault = tmp_path / "vault"
    vault.mkdir()
    service = JournalCaptureService(store, JournalContentAdapter(vault))
    monkeypatch.setattr(journal_api, "_runtime", (sources, store, service))
    app = Flask("journal-day-range")
    journal_api.register_routes(app)
    return app.test_client()


def test_view_refuses_a_day_that_has_not_started(tmp_path, monkeypatch):
    client = _view_client(tmp_path, monkeypatch)
    today = date.fromisoformat(journal_api.current_day()["localDate"])

    response = client.get(f"/api/journal/view?day={(today + timedelta(days=1)).isoformat()}")
    assert response.status_code == 400, response.json
    assert response.json["error"]["code"] == "journal_day_unavailable"

    far = client.get(f"/api/journal/view?day={(today + timedelta(days=400)).isoformat()}")
    assert far.status_code == 400, far.json
    assert far.json["error"]["code"] == "journal_day_unavailable"


def test_view_still_reaches_today_and_days_behind_it(tmp_path, monkeypatch):
    client = _view_client(tmp_path, monkeypatch)
    today = date.fromisoformat(journal_api.current_day()["localDate"])

    current = client.get(f"/api/journal/view?day={today.isoformat()}")
    assert current.status_code == 200, current.json
    assert current.json["view"]["day"]["localDate"] == today.isoformat()

    behind = (today - timedelta(days=365)).isoformat()
    older = client.get(f"/api/journal/view?day={behind}")
    assert older.status_code == 200, older.json
    assert older.json["view"]["day"]["localDate"] == behind
