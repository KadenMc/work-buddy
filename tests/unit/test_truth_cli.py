"""Focused contract tests for the direct ``wbuddy truth`` CLI."""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from work_buddy.cli import dispatch
from work_buddy.cli import truth as truth_cli
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor
from work_buddy.truth.events import TruthEventEmission
from work_buddy.truth.identity import new_id, utc_now
from work_buddy.truth.lifecycle import TruthLifecycle, hash_context
from work_buddy.truth.review import compose_claim_review
from work_buddy.truth.store import TruthStore


HUMAN = Actor("human", "test-human")


def _profile() -> dict:
    return {
        "store_id": new_id(),
        "profile": "cli-test",
        "title": "CLI test store",
        "allowed_claim_kinds": ["fact", "preference"],
        "required_fields": {},
        "gate": {
            "rejected_content": "redact",
            "confirmation_surfaces": ["dashboard", "cli"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": True,
    }


@pytest.fixture
def cli_store(tmp_path: Path) -> TruthStore:
    root = tmp_path / "scope"
    root.mkdir()
    return TruthStore.create(root, _profile())


@pytest.fixture(autouse=True)
def standalone_cli(monkeypatch):
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "wbuddy-cli")
    for name in (
        "CODEX_THREAD_ID",
        "CLAUDE_CODE_ENTRYPOINT",
        "WORK_BUDDY_MODEL",
        "CODEX_MODEL",
        "CLAUDE_MODEL",
        "WORK_BUDDY_HARNESS_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(truth_cli, "_touch_registry", lambda store, registry=None: None)
    monkeypatch.setattr(
        truth_cli,
        "emit_truth_event",
        lambda event_type, **kwargs: TruthEventEmission("event-test", True),
    )


def _json_result(capsys, argv: list[str]) -> tuple[int, dict]:
    rc = dispatch.main(argv)
    captured = capsys.readouterr()
    assert captured.err == ""
    return rc, json.loads(captured.out)


def _proposed(store: TruthStore, proposition: str = "The sky is blue"):
    return store.propose_claim(
        proposition=proposition,
        claim_kind="fact",
        actor=HUMAN,
    ).claim


def _add_receipt(store: TruthStore, claim, text: str = "supporting receipt"):
    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///tmp/receipt.txt",
        actor=HUMAN,
        acquisition_method="paste",
        content=text,
    )
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact=text),
        actor=HUMAN,
    )
    return store.add_link(
        from_claim_id=claim.id,
        link_type="supports_span",
        to_kind="evidence_span",
        to_ref=span.id,
        actor=HUMAN,
    )


def test_store_and_json_work_before_or_after_verb(cli_store, capsys):
    root = str(cli_store.paths.root)
    first_rc, first = _json_result(
        capsys,
        ["truth", "--store", root, "--json", "query"],
    )
    second_rc, second = _json_result(
        capsys,
        ["truth", "query", "--store", root, "--json"],
    )
    assert first_rc == second_rc == 0
    assert first["result"] == second["result"]
    assert first["store"]["path"] == str(cli_store.paths.sidecar)


def test_discovers_nearest_ancestor(cli_store, tmp_path, monkeypatch, capsys):
    nested = cli_store.paths.root / "one" / "two"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    rc, payload = _json_result(capsys, ["truth", "query", "--json"])

    assert rc == 0
    assert payload["store"]["store_id"] == cli_store.store_id


def test_capture_can_mark_quote_span(cli_store, monkeypatch, capsys):
    emitted = []

    def emit(event_type, **kwargs):
        emitted.append((event_type, kwargs))
        return TruthEventEmission(f"event-{len(emitted)}", True)

    monkeypatch.setattr(truth_cli, "emit_truth_event", emit)
    rc, payload = _json_result(
        capsys,
        [
            "truth",
            "capture",
            "--store",
            str(cli_store.paths.root),
            "--kind",
            "document",
            "--source-locator",
            "file:///tmp/source.txt",
            "--acquisition-method",
            "paste",
            "--content",
            "alpha beta gamma",
            "--quote",
            "beta",
            "--json",
        ],
    )

    assert rc == 0
    assert payload["result"]["trust_class"] == "user_authored"
    span = cli_store.get_span(payload["result"]["span_id"])
    assert span is not None
    assert span.quote_exact == "beta"
    evidence = cli_store.get_evidence(span.evidence_id)
    assert evidence is not None
    locator_meta = json.loads(evidence.meta_json)
    assert locator_meta["locator_scheme"] == "file"
    assert locator_meta["verifiability_class"] == "A"
    assert locator_meta["integrity_recipe"]["method"] == "verify_local_snapshot_bytes"
    assert [item[0] for item in emitted] == [
        "truth.evidence_captured",
        "truth.span_marked",
    ]
    assert emitted[0][1]["subject_id"] == evidence.id
    assert emitted[1][1]["subject_id"] == span.id
    assert payload["result"]["events"] == [
        {"event_id": "event-1", "published": True, "error": None},
        {"event_id": "event-2", "published": True, "error": None},
    ]


def test_invalid_quote_does_not_capture_partial_evidence(
    cli_store,
    monkeypatch,
    capsys,
):
    emitted = []
    monkeypatch.setattr(
        truth_cli,
        "emit_truth_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )
    rc, payload = _json_result(
        capsys,
        [
            "truth",
            "capture",
            "--store",
            str(cli_store.paths.root),
            "--kind",
            "document",
            "--source-locator",
            "file:///tmp/source.txt",
            "--acquisition-method",
            "paste",
            "--content",
            "alpha beta gamma",
            "--quote",
            "missing",
            "--json",
        ],
    )

    assert rc == 1
    assert payload["ok"] is False
    conn = cli_store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    finally:
        conn.close()
    assert emitted == []


def test_propose_attaches_support_without_duplicate_link(
    cli_store,
    monkeypatch,
    capsys,
):
    emitted = []

    def emit(event_type, **kwargs):
        emitted.append((event_type, kwargs))
        return TruthEventEmission(f"proposal-{len(emitted)}", True)

    monkeypatch.setattr(truth_cli, "emit_truth_event", emit)
    _, captured = _json_result(
        capsys,
        [
            "truth",
            "capture",
            "--store",
            str(cli_store.paths.root),
            "--kind",
            "document",
            "--source-locator",
            "file:///tmp/source.txt",
            "--acquisition-method",
            "paste",
            "--content",
            "A supported assertion.",
            "--quote",
            "supported assertion",
            "--json",
        ],
    )
    span_id = captured["result"]["span_id"]
    argv = [
        "truth",
        "propose",
        "--store",
        str(cli_store.paths.root),
        "--proposition",
        "The assertion is supported",
        "--kind",
        "fact",
        "--support-span",
        span_id,
        "--json",
    ]

    first_rc, first = _json_result(capsys, argv)
    second_rc, second = _json_result(capsys, argv)

    assert first_rc == second_rc == 0
    assert first["result"]["created"] is True
    assert second["result"]["created"] is False
    assert first["result"]["events"][0]["event_id"] == "proposal-3"
    assert second["result"]["events"] == []
    assert first["result"]["support_link_ids"] == second["result"]["support_link_ids"]
    assert [item[0] for item in emitted] == [
        "truth.evidence_captured",
        "truth.span_marked",
        "truth.claim_proposed",
    ]
    conn = cli_store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM claim_links").fetchone()[0] == 1
    finally:
        conn.close()


def test_agent_write_with_incomplete_provenance_fails_closed(
    cli_store,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("CODEX_THREAD_ID", "agent-session")
    monkeypatch.setattr(truth_cli, "_session_manifest", lambda session_id: {})

    rc, payload = _json_result(
        capsys,
        [
            "truth",
            "capture",
            "--store",
            str(cli_store.paths.root),
            "--kind",
            "document",
            "--source-locator",
            "file:///tmp/source.txt",
            "--acquisition-method",
            "paste",
            "--content",
            "content",
            "--json",
        ],
    )

    assert rc == 1
    assert "missing producer identity fields: model" in payload["error"]["message"]
    conn = cli_store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    finally:
        conn.close()


def test_agent_write_prefers_manifest_identity_and_ignores_placeholders(
    cli_store,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("CODEX_THREAD_ID", "agent-session")
    monkeypatch.setenv("WORK_BUDDY_MODEL", " unknown ")
    monkeypatch.setenv("WORK_BUDDY_HARNESS_ID", "N/A")
    monkeypatch.setattr(
        truth_cli,
        "_session_manifest",
        lambda session_id: {
            "session_id": session_id,
            "model": "gpt-manifest",
            "harness_id": "codexcli",
        },
    )

    rc, payload = _json_result(
        capsys,
        [
            "truth",
            "capture",
            "--store",
            str(cli_store.paths.root),
            "--kind",
            "document",
            "--source-locator",
            "file:///tmp/source.txt",
            "--acquisition-method",
            "paste",
            "--content",
            "manifest-backed content",
            "--json",
        ],
    )

    assert rc == 0
    evidence = cli_store.get_evidence(payload["result"]["evidence_id"])
    meta = json.loads(evidence.meta_json)
    assert meta["model"] == "gpt-manifest"
    assert meta["harness"] == "codexcli"
    assert meta["model_source"] == "session_manifest"


@pytest.mark.parametrize(
    ("environment_name", "environment_value", "message"),
    [
        (
            "WORK_BUDDY_MODEL",
            "gpt-environment",
            "environment model does not match the session manifest model",
        ),
        (
            "WORK_BUDDY_HARNESS_ID",
            "claudecode",
            "environment harness does not match the session manifest harness",
        ),
    ],
)
def test_agent_write_rejects_environment_manifest_identity_mismatch(
    monkeypatch,
    environment_name,
    environment_value,
    message,
):
    monkeypatch.setenv("CODEX_THREAD_ID", "agent-session")
    monkeypatch.setenv(environment_name, environment_value)
    monkeypatch.setattr(
        truth_cli,
        "_session_manifest",
        lambda session_id: {
            "session_id": session_id,
            "model": "gpt-manifest",
            "harness_id": "codexcli",
        },
    )

    with pytest.raises(truth_cli.TruthError, match=message):
        truth_cli._actor_for_write()


def test_agent_write_labels_environment_model_fallback_as_caller_asserted(
    cli_store,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("CODEX_THREAD_ID", "agent-session")
    monkeypatch.setenv("WORK_BUDDY_MODEL", "gpt-environment")
    monkeypatch.setattr(
        truth_cli,
        "_session_manifest",
        lambda session_id: {
            "session_id": session_id,
            "model": "unspecified",
            "harness_id": "codexcli",
        },
    )

    rc, payload = _json_result(
        capsys,
        [
            "truth",
            "capture",
            "--store",
            str(cli_store.paths.root),
            "--kind",
            "document",
            "--source-locator",
            "file:///tmp/source.txt",
            "--acquisition-method",
            "paste",
            "--content",
            "caller-asserted content",
            "--json",
        ],
    )

    assert rc == 0
    evidence = cli_store.get_evidence(payload["result"]["evidence_id"])
    meta = json.loads(evidence.meta_json)
    assert meta["model"] == "gpt-environment"
    assert meta["harness"] == "codexcli"
    assert meta["model_source"] == "caller_asserted"


def test_agent_write_does_not_invent_harness_identity(
    cli_store,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("CODEX_THREAD_ID", "agent-session")
    monkeypatch.setenv("WORK_BUDDY_MODEL", "gpt-test")
    monkeypatch.setattr(truth_cli, "_session_manifest", lambda session_id: {})
    emitted = []
    monkeypatch.setattr(
        truth_cli,
        "emit_truth_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )

    rc, payload = _json_result(
        capsys,
        [
            "truth",
            "capture",
            "--store",
            str(cli_store.paths.root),
            "--kind",
            "document",
            "--source-locator",
            "file:///tmp/source.txt",
            "--acquisition-method",
            "paste",
            "--content",
            "content",
            "--json",
        ],
    )

    assert rc == 1
    assert payload["error"]["message"].endswith("harness")
    conn = cli_store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    finally:
        conn.close()
    assert emitted == []


def test_non_tty_confirm_refuses_piped_yes_without_writing(
    cli_store,
    monkeypatch,
    capsys,
):
    claim = _proposed(cli_store)
    emitted = []
    monkeypatch.setattr(
        truth_cli,
        "emit_truth_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )
    monkeypatch.setattr(truth_cli, "_is_interactive_tty", lambda: False)
    monkeypatch.setattr(
        truth_cli,
        "_prompt_confirmation",
        lambda *args, **kwargs: pytest.fail("prompt must not run"),
    )

    rc, payload = _json_result(
        capsys,
        [
            "truth",
            "confirm",
            claim.id,
            "--store",
            str(cli_store.paths.root),
            "--json",
        ],
    )

    assert rc == 1
    assert "interactive TTY" in payload["error"]["message"]
    assert TruthLifecycle(cli_store).latest_status(claim.id).status == "proposed"
    conn = cli_store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM gestures").fetchone()[0] == 0
    finally:
        conn.close()
    assert emitted == []


def test_agent_context_cannot_use_interactive_tty_confirmation(
    cli_store,
    monkeypatch,
    capsys,
):
    claim = _proposed(cli_store)
    monkeypatch.setenv("CODEX_THREAD_ID", "agent-session")
    monkeypatch.setattr(truth_cli, "_is_interactive_tty", lambda: True)
    prompted = []

    def answer_yes(*args, **kwargs):
        prompted.append((args, kwargs))
        return True

    monkeypatch.setattr(truth_cli, "_prompt_confirmation", answer_yes)
    emitted = []
    monkeypatch.setattr(
        truth_cli,
        "emit_truth_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )

    rc, payload = _json_result(
        capsys,
        [
            "truth",
            "confirm",
            claim.id,
            "--store",
            str(cli_store.paths.root),
            "--json",
        ],
    )

    assert rc == 1
    assert "MCP per-invocation consent" in payload["error"]["message"]
    assert "--gesture <id>" in payload["error"]["message"]
    assert prompted == []
    assert emitted == []
    assert TruthLifecycle(cli_store).latest_status(claim.id).status == "proposed"
    conn = cli_store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM gestures").fetchone()[0] == 0
    finally:
        conn.close()


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_interactive_confirm_renders_once_and_mints_cli_gesture(
    cli_store,
    monkeypatch,
):
    claim = _proposed(cli_store)
    fake_in = _TTY("yes\n")
    fake_out = _TTY()
    monkeypatch.setattr(sys, "stdin", fake_in)
    monkeypatch.setattr(sys, "stdout", fake_out)

    rc = dispatch.main(
        ["truth", "confirm", claim.id, "--store", str(cli_store.paths.root)]
    )

    assert rc == 0
    assert fake_out.getvalue().count("Truth decision: confirm") == 1
    assert fake_out.getvalue().count("Confirm this claim? [y/N]") == 1
    assert TruthLifecycle(cli_store).latest_status(claim.id).status == "confirmed"
    conn = cli_store.connect()
    try:
        row = conn.execute("SELECT * FROM gestures").fetchone()
    finally:
        conn.close()
    assert row["surface"] == "cli"
    assert row["context_sha256"]
    assert row["consumed_at"]


def test_interactive_confirm_rejects_receipt_drift_during_prompt(
    cli_store,
    monkeypatch,
    capsys,
):
    claim = _proposed(cli_store)
    monkeypatch.setattr(truth_cli, "_is_interactive_tty", lambda: True)

    def mutate_then_confirm(body, *, json_mode):
        assert "Active receipts: 0" in body
        _add_receipt(cli_store, claim)
        return True

    monkeypatch.setattr(truth_cli, "_prompt_confirmation", mutate_then_confirm)

    rc, payload = _json_result(
        capsys,
        [
            "truth",
            "confirm",
            claim.id,
            "--store",
            str(cli_store.paths.root),
            "--json",
        ],
    )

    assert rc == 1
    assert "review changed" in payload["error"]["message"]
    assert TruthLifecycle(cli_store).latest_status(claim.id).status == "proposed"
    conn = cli_store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM gestures").fetchone()[0] == 0
    finally:
        conn.close()


def _dashboard_gesture(
    store: TruthStore,
    claim,
    *,
    at: str | None = None,
    expires_at: str | None = None,
):
    review = compose_claim_review(store, claim.id, action="confirm")
    return TruthLifecycle(store).mint_gesture(
        subject_ref=claim.id,
        actor=Actor("human", "dashboard-user"),
        surface="dashboard",
        kind="confirm",
        displayed_payload_sha256=claim.canonical_sha256,
        context_sha256=review.context_sha256,
        at=at,
        expires_at=expires_at,
    )


def test_deferred_gesture_confirms_without_prompt(cli_store, monkeypatch, capsys):
    claim = _proposed(cli_store)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    gesture = _dashboard_gesture(cli_store, claim, expires_at=expires)
    monkeypatch.setattr(
        truth_cli,
        "_prompt_confirmation",
        lambda *args, **kwargs: pytest.fail("prompt must not run"),
    )
    emitted = []

    def emit(event_type, **kwargs):
        emitted.append((event_type, kwargs))
        return TruthEventEmission("confirmation-event", True)

    monkeypatch.setattr(truth_cli, "emit_truth_event", emit)

    rc, payload = _json_result(
        capsys,
        [
            "truth",
            "confirm",
            claim.id,
            "--gesture",
            gesture.id,
            "--store",
            str(cli_store.paths.root),
            "--json",
        ],
    )

    assert rc == 0
    assert payload["result"]["status"] == "confirmed"
    assert payload["result"]["events"] == [
        {
            "event_id": "confirmation-event",
            "published": True,
            "error": None,
        }
    ]
    assert emitted[0][0] == "truth.claim_confirmed"
    assert emitted[0][1]["data"]["superseded"] == []
    assert emitted[0][1]["data"]["needs_review_event_id"] is None
    conn = cli_store.connect()
    try:
        consumed = conn.execute(
            "SELECT consumed_at FROM gestures WHERE id = ?",
            (gesture.id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert consumed is not None


def test_deferred_gesture_honors_expiry(cli_store, monkeypatch, capsys):
    claim = _proposed(cli_store)
    emitted = []
    monkeypatch.setattr(
        truth_cli,
        "emit_truth_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )
    gesture = _dashboard_gesture(
        cli_store,
        claim,
        at="2020-01-01T00:00:00+00:00",
        expires_at="2020-01-01T00:01:00+00:00",
    )

    rc, payload = _json_result(
        capsys,
        [
            "truth",
            "confirm",
            claim.id,
            "--gesture",
            gesture.id,
            "--store",
            str(cli_store.paths.root),
            "--json",
        ],
    )

    assert rc == 1
    assert "expired" in payload["error"]["message"]
    conn = cli_store.connect()
    try:
        consumed = conn.execute(
            "SELECT consumed_at FROM gestures WHERE id = ?",
            (gesture.id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert consumed is None
    assert emitted == []


def test_deferred_gesture_rejects_current_receipt_drift(
    cli_store,
    monkeypatch,
    capsys,
):
    claim = _proposed(cli_store)
    gesture = _dashboard_gesture(cli_store, claim)
    _add_receipt(cli_store, claim)
    emitted = []
    monkeypatch.setattr(
        truth_cli,
        "emit_truth_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )

    rc, payload = _json_result(
        capsys,
        [
            "truth",
            "confirm",
            claim.id,
            "--gesture",
            gesture.id,
            "--store",
            str(cli_store.paths.root),
            "--json",
        ],
    )

    assert rc == 1
    assert "context does not match" in payload["error"]["message"]
    assert TruthLifecycle(cli_store).latest_status(claim.id).status == "proposed"
    conn = cli_store.connect()
    try:
        consumed = conn.execute(
            "SELECT consumed_at FROM gestures WHERE id = ?",
            (gesture.id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert consumed is None
    assert emitted == []


def test_event_publication_exception_does_not_change_cli_success(
    cli_store,
    monkeypatch,
    capsys,
):
    def fail_publication(*args, **kwargs):
        raise RuntimeError("event spine unavailable")

    monkeypatch.setattr(truth_cli, "emit_truth_event", fail_publication)
    rc, payload = _json_result(
        capsys,
        [
            "truth",
            "capture",
            "--store",
            str(cli_store.paths.root),
            "--kind",
            "document",
            "--source-locator",
            "file:///tmp/durable.txt",
            "--acquisition-method",
            "paste",
            "--content",
            "durable content",
            "--json",
        ],
    )

    assert rc == 0
    assert payload["result"]["events"] == [
        {
            "event_id": None,
            "published": False,
            "error": "event spine unavailable",
        }
    ]
    assert cli_store.get_evidence(payload["result"]["evidence_id"]) is not None


def test_query_views_have_stable_envelopes(cli_store, monkeypatch, capsys):
    monkeypatch.setattr(
        truth_cli,
        "emit_truth_event",
        lambda *args, **kwargs: pytest.fail("query must not emit events"),
    )
    claim = _proposed(cli_store)
    context = {"receipt": "test"}
    lifecycle = TruthLifecycle(cli_store)
    gesture = lifecycle.mint_gesture(
        subject_ref=claim.id,
        actor=HUMAN,
        surface="cli",
        kind="confirm",
        displayed_payload_sha256=claim.canonical_sha256,
        context_sha256=hash_context(context),
    )
    lifecycle.confirm_claim(
        claim_id=claim.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        expected_context_sha256=gesture.context_sha256,
    )
    as_of = utc_now()

    for extra, expected in (
        ([], "current"),
        (["--view", "as-of", "--belief-at", as_of], "as-of"),
        (["--view", "needs-review"], "needs-review"),
        (["--view", "conflicts"], "conflicts"),
    ):
        rc, payload = _json_result(
            capsys,
            [
                "truth",
                "query",
                "--store",
                str(cli_store.paths.root),
                *extra,
                "--json",
            ],
        )
        assert rc == 0
        assert payload["result"]["view"] == expected
        assert isinstance(payload["result"]["items"], list)
        assert payload["result"]["events"] == []


def test_migrate_local_store(cli_store, monkeypatch, capsys):
    monkeypatch.setattr(
        truth_cli,
        "emit_truth_event",
        lambda *args, **kwargs: pytest.fail("migrate must not emit events"),
    )
    rc, payload = _json_result(
        capsys,
        [
            "truth",
            "migrate",
            "--store",
            str(cli_store.paths.root),
            "--json",
        ],
    )

    assert rc == 0
    assert payload["result"]["failed"] == 0
    assert payload["result"]["stores"][0]["status"] == "ok"
    assert payload["result"]["events"] == []


def test_migrate_all_reports_unreachable_rows(cli_store, monkeypatch, capsys):
    class Registry:
        def list_stores(self, *, refresh=True):
            assert refresh is True
            return (
                SimpleNamespace(path=cli_store.paths.sidecar, reachable=True),
                SimpleNamespace(path=Path("Z:/missing/.wb-truth"), reachable=False),
            )

        def touch(self, store):
            return None

    registry = Registry()
    monkeypatch.setattr(truth_cli, "_registry_class", lambda: lambda: registry)

    rc, payload = _json_result(
        capsys,
        ["truth", "migrate", "--all", "--json"],
    )

    assert rc == 1
    assert payload["result"]["failed"] == 1
    assert payload["result"]["stores"][1]["status"] == "unreachable"


@pytest.mark.parametrize("forbidden", ["--yes", "--force"])
def test_confirm_has_no_bypass_flag(forbidden, cli_store):
    claim = _proposed(cli_store)
    assert dispatch.main(
        [
            "truth",
            "confirm",
            claim.id,
            forbidden,
            "--store",
            str(cli_store.paths.root),
        ]
    ) == 2
