from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from work_buddy.sources import ActorRef
from work_buddy.sources.cutover_receipt import (
    CutoverAuthorizationReceiptError,
    consume_cutover_authorization_receipt,
    issue_cutover_authorization_receipt,
)


NOW = datetime(2026, 8, 27, 16, 0, tzinfo=UTC)


def _human() -> ActorRef:
    return ActorRef(
        "authority-test-0001",
        "local-human-test-0001",
        "human",
        "tenant-test-0001",
    )


def _scope(suffix: str = "a") -> dict:
    return {
        "schema": "wb.private-cutover-scope/v1",
        "domains": {
            "journal": {
                "cohortId": f"journal-{suffix}-0001",
                "inventorySha256": "1" * 64,
            },
            "projects": {
                "cohortId": f"projects-{suffix}-0001",
                "inventorySha256": "2" * 64,
            },
            "personal_knowledge": {
                "cohortId": f"personal-{suffix}-0001",
                "inventorySha256": "3" * 64,
            },
            "contracts": {
                "cohortId": f"contracts-{suffix}-0001",
                "inventorySha256": "4" * 64,
            },
        },
    }


def _issue(root: Path, *, scope: dict | None = None, expires=None):
    return issue_cutover_authorization_receipt(
        root=root,
        scope=scope or _scope(),
        enrolled_human=_human(),
        authorization_fingerprint="a" * 64,
        consent_request_id="consent-request-test-0001",
        expires_at=expires or NOW + timedelta(minutes=30),
        now=NOW,
    )


def test_receipt_is_single_consumption_with_same_id_crash_replay(tmp_path: Path):
    issued = _issue(tmp_path)

    first = consume_cutover_authorization_receipt(
        issued.manifest_path,
        authorization_root=tmp_path,
        expected_scope=_scope(),
        consumption_id="cutover-run-test-0001",
        now=NOW + timedelta(minutes=1),
    )
    replay = consume_cutover_authorization_receipt(
        issued.manifest_path,
        authorization_root=tmp_path,
        expected_scope=_scope(),
        consumption_id="cutover-run-test-0001",
        now=NOW + timedelta(hours=2),
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert first.authorization == replay.authorization
    assert first.authorization.inputter == _human()
    assert first.authorization.issuer.kind == "service"
    assert first.authorization.principal.kind == "service"
    assert first.authorization.authorization_fingerprint == "a" * 64
    with pytest.raises(CutoverAuthorizationReceiptError, match="already consumed"):
        consume_cutover_authorization_receipt(
            issued.manifest_path,
            authorization_root=tmp_path,
            expected_scope=_scope(),
            consumption_id="cutover-run-test-0002",
            now=NOW + timedelta(hours=2),
        )


def test_receipt_rejects_manifest_tamper_expiry_and_scope_mismatch(tmp_path: Path):
    tampered = _issue(tmp_path / "tampered", scope=_scope("tamper"))
    payload = json.loads(tampered.manifest_path.read_text(encoding="utf-8"))
    payload["namespace"] = "changed"
    tampered.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CutoverAuthorizationReceiptError, match="changed"):
        consume_cutover_authorization_receipt(
            tampered.manifest_path,
            authorization_root=tmp_path / "tampered",
            expected_scope=_scope("tamper"),
            consumption_id="cutover-run-tamper-0001",
            now=NOW + timedelta(minutes=1),
        )

def test_receipt_consumer_rejects_a_caller_selected_adjacent_ledger(tmp_path: Path):
    trusted_root = tmp_path / "trusted"
    issued = _issue(trusted_root)
    forged_root = tmp_path / "caller-selected"
    forged_root.mkdir()
    forged_manifest = forged_root / issued.manifest_path.name
    forged_manifest.write_bytes(issued.manifest_path.read_bytes())
    (forged_root / "receipts.db").write_bytes(
        (trusted_root / "receipts.db").read_bytes()
    )

    with pytest.raises(CutoverAuthorizationReceiptError, match="trusted receipt ledger"):
        consume_cutover_authorization_receipt(
            forged_manifest,
            authorization_root=trusted_root,
            expected_scope=_scope(),
            consumption_id="cutover-run-forged-0001",
            now=NOW + timedelta(minutes=1),
        )


def test_expired_unconsumed_receipt_can_be_superseded_but_consumed_scope_cannot(
    tmp_path: Path,
):
    root = tmp_path / "supersede"
    old = _issue(root, expires=NOW + timedelta(minutes=1))
    replacement = issue_cutover_authorization_receipt(
        root=root,
        scope=_scope(),
        enrolled_human=_human(),
        authorization_fingerprint="c" * 64,
        consent_request_id="consent-request-replacement-0001",
        expires_at=NOW + timedelta(minutes=32),
        now=NOW + timedelta(minutes=2),
    )

    assert replacement.manifest_id != old.manifest_id
    assert replacement.manifest_path.is_file()
    assert not old.manifest_path.exists()
    consumed = consume_cutover_authorization_receipt(
        replacement.manifest_path,
        authorization_root=root,
        expected_scope=_scope(),
        consumption_id="cutover-run-replacement-0001",
        now=NOW + timedelta(minutes=3),
    )
    assert consumed.replayed is False
    with pytest.raises(CutoverAuthorizationReceiptError, match="already consumed"):
        issue_cutover_authorization_receipt(
            root=root,
            scope=_scope(),
            enrolled_human=_human(),
            authorization_fingerprint="d" * 64,
            consent_request_id="consent-request-after-consume-0001",
            expires_at=NOW + timedelta(minutes=50),
            now=NOW + timedelta(minutes=40),
        )

    expired = _issue(
        tmp_path / "expired",
        scope=_scope("expired"),
        expires=NOW + timedelta(minutes=1),
    )
    with pytest.raises(CutoverAuthorizationReceiptError, match="expired"):
        consume_cutover_authorization_receipt(
            expired.manifest_path,
            authorization_root=tmp_path / "expired",
            expected_scope=_scope("expired"),
            consumption_id="cutover-run-expired-0001",
            now=NOW + timedelta(minutes=2),
        )

    mismatch = _issue(tmp_path / "mismatch", scope=_scope("expected"))
    with pytest.raises(CutoverAuthorizationReceiptError, match="scope changed"):
        consume_cutover_authorization_receipt(
            mismatch.manifest_path,
            authorization_root=tmp_path / "mismatch",
            expected_scope=_scope("different"),
            consumption_id="cutover-run-mismatch-0001",
            now=NOW + timedelta(minutes=1),
        )
