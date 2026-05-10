"""Regression test for the wrapper-aware pre-verify path in RetrySweep.

When ``obsidian_retry`` (or generic ``retry``) is queued with a
``pwu_carrier`` pointing to the FIRST of several effects (e.g. the note
file written by ``task_create``), the sweep's ``_pre_verify_pwu`` must
not fall through to single-effect verify on the carrier path. Single-
effect verify would see the note exists with the right sha256, declare
``verified``, and let the sweep fire ``retry_success`` — even when the
SECOND effect (the master-list line) had never landed.

The wrapper-aware contract: when the queued capability is a wrapper
(``retry`` / ``obsidian_retry``) and its ``params.operation_id``
references an inner op, the verifier looks up the inner op's capability
and walks ITS effects manifest. Only when the inner record can't be
resolved does it fall back to the single-effect path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from work_buddy.config import load_config
from work_buddy.sidecar.retry_sweep import RetrySweep


@pytest.fixture
def vault(tmp_path: Path, monkeypatch) -> Path:
    """Point ``vault_root`` at a temp dir so the filesystem verifier
    reads from our test files, not the real vault."""
    cfg = dict(load_config())
    cfg["vault_root"] = str(tmp_path)
    monkeypatch.setattr(
        "work_buddy.obsidian.post_write_verify.load_config",
        lambda: cfg,
    )
    return tmp_path


@pytest.fixture
def operations_dir(tmp_path: Path, monkeypatch) -> Path:
    """Redirect retry_sweep's ``_get_operations_dir`` to a temp dir so
    we can persist synthetic op records without touching real state."""
    ops_dir = tmp_path / "operations"
    ops_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "work_buddy.sidecar.retry_sweep._get_operations_dir",
        lambda: ops_dir,
    )
    return ops_dir


def _make_note(vault: Path, uuid: str, content: str) -> tuple[str, str]:
    """Create a note file under the test vault, return (vault-relative
    path, sha256:<hex> content hint matching what the bridge produces)."""
    note_path_rel = f"tasks/notes/{uuid}.md"
    note_abs = vault / note_path_rel
    note_abs.parent.mkdir(parents=True, exist_ok=True)
    note_abs.write_text(content, encoding="utf-8")
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return note_path_rel, f"sha256:{sha}"


def _seed_task_create_idem_cache(
    vault: Path,
    monkeypatch,
    *,
    task_text: str,
    summary: str,
    task_id: str,
    note_uuid: str,
) -> dict:
    """Pre-populate the task_create idempotency cache so the resolver
    returns our forged ``task_id`` and ``note_uuid``. Returns the params
    dict the cache key was computed from (for use on the inner op
    record)."""
    from work_buddy.obsidian.tasks.mutations import (
        _create_task_idempotency_key,
        _record_idempotent_create_ids,
    )

    idem_dir = vault / "_idem"
    idem_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations._idempotency_dir",
        lambda: idem_dir,
    )

    params = {
        "task_text": task_text,
        "summary": summary,
        "urgency": "medium",
        "project": None,
        "contract": None,
        "tags": [],
        "due_date": None,
    }
    key = _create_task_idempotency_key(
        task_text=params["task_text"],
        summary=params["summary"],
        project=params["project"],
        urgency=params["urgency"],
        contract=params["contract"],
        tags=params["tags"],
        due_date=params["due_date"],
    )
    _record_idempotent_create_ids(key, task_id, note_uuid)
    return params


def test_obsidian_retry_wrapper_uses_inner_task_create_manifest(
    vault: Path, operations_dir: Path, monkeypatch,
) -> None:
    """When a queued ``obsidian_retry`` op carries a PWU carrier for the
    note file (first effect), the sweep looks up the inner
    ``task_create`` op, walks its declared effects manifest, and
    proceeds with normal replay because the master-list witness is
    missing. ``_pre_verify_pwu`` must return ``None`` here — returning
    a success-with-warning based on the note's existence alone would
    let a partial-state write fire ``retry_success``.
    """
    # First effect (note) landed on disk with matching sha256.
    note_uuid = "11111111-1111-1111-1111-111111111111"
    note_content = "PROBE-TASK-TEXT body of the note"
    note_path_rel, sha_hint = _make_note(vault, note_uuid, note_content)

    # Second effect (master-list) does NOT contain the task_id.
    fake_task_id = "t-NOTINMASTER"
    (vault / "tasks").mkdir(parents=True, exist_ok=True)
    (vault / "tasks" / "master-task-list.md").write_text(
        "- [ ] some other task 🆔 t-otherthing\n",
        encoding="utf-8",
    )

    # Pre-seed the create_task idempotency cache so the effect manifest's
    # resolver returns our forged ids when walking the inner manifest.
    params = _seed_task_create_idem_cache(
        vault,
        monkeypatch,
        task_text="PROBE-TASK-TEXT",
        summary="probe summary",
        task_id=fake_task_id,
        note_uuid=note_uuid,
    )

    # Persist the INNER op record (the task_create that obsidian_retry
    # would replay).
    inner_op = {
        "operation_id": "op_inner_taskcreate",
        "name": "task_create",
        "params": params,
        "status": "failed",
    }
    (operations_dir / f"{inner_op['operation_id']}.json").write_text(
        json.dumps(inner_op), encoding="utf-8"
    )

    # The WRAPPER record the sweep actually sees.
    wrapper_record = {
        "operation_id": "op_wrapper_obsidian_retry",
        "name": "obsidian_retry",  # no manifest of its own
        "params": {"operation_id": inner_op["operation_id"]},
        "pwu_carrier": {
            "path": note_path_rel,
            "content_hint": sha_hint,
            "write_mode": "replace",
        },
    }

    verdict = RetrySweep(config={})._pre_verify_pwu(
        wrapper_record["pwu_carrier"], record=wrapper_record,
    )

    assert verdict is None, (
        "Wrapper-style record (obsidian_retry) should defer to inner "
        "capability's effects manifest. Got: "
        f"{verdict!r}"
    )


def test_generic_retry_wrapper_uses_inner_manifest(
    vault: Path, operations_dir: Path, monkeypatch,
) -> None:
    """Same as the obsidian_retry case but for the generic ``retry``
    capability. Both replay an inner op by id; both need the inner
    manifest for multi-effect verify."""
    note_uuid = "33333333-3333-3333-3333-333333333333"
    note_content = "GENERIC-RETRY body"
    note_path_rel, sha_hint = _make_note(vault, note_uuid, note_content)

    (vault / "tasks").mkdir(parents=True, exist_ok=True)
    (vault / "tasks" / "master-task-list.md").write_text("", encoding="utf-8")

    params = _seed_task_create_idem_cache(
        vault,
        monkeypatch,
        task_text="GENERIC-RETRY",
        summary="probe summary 2",
        task_id="t-GENERIC-NOTINMASTER",
        note_uuid=note_uuid,
    )

    inner_op = {
        "operation_id": "op_inner_for_generic_retry",
        "name": "task_create",
        "params": params,
        "status": "failed",
    }
    (operations_dir / f"{inner_op['operation_id']}.json").write_text(
        json.dumps(inner_op), encoding="utf-8"
    )

    wrapper_record = {
        "operation_id": "op_wrapper_generic_retry",
        "name": "retry",  # generic retry wrapper
        "params": {"operation_id": inner_op["operation_id"]},
        "pwu_carrier": {
            "path": note_path_rel,
            "content_hint": sha_hint,
            "write_mode": "replace",
        },
    }

    verdict = RetrySweep(config={})._pre_verify_pwu(
        wrapper_record["pwu_carrier"], record=wrapper_record,
    )

    assert verdict is None, (
        "Generic retry wrapper should also defer to inner capability's "
        f"effects manifest. Got: {verdict!r}"
    )


def test_wrapper_falls_back_to_single_effect_when_inner_op_missing(
    vault: Path, operations_dir: Path,
) -> None:
    """If the inner op record can't be loaded, preserve the existing
    single-effect verify behavior on the carrier. Better than blocking
    on a missing reference — same outcome the sweep would have had
    before the wrapper-aware path existed."""
    note_uuid = "22222222-2222-2222-2222-222222222222"
    note_content = "missing-inner-op body"
    note_path_rel, sha_hint = _make_note(vault, note_uuid, note_content)

    wrapper_record = {
        "operation_id": "op_wrapper_orphan",
        "name": "obsidian_retry",
        "params": {"operation_id": "op_does_not_exist"},
        "pwu_carrier": {
            "path": note_path_rel,
            "content_hint": sha_hint,
            "write_mode": "replace",
        },
    }

    verdict = RetrySweep(config={})._pre_verify_pwu(
        wrapper_record["pwu_carrier"], record=wrapper_record,
    )

    # Inner op missing → single-effect fallback → note exists with right
    # sha256 → "verified" → success-with-warning.
    assert verdict is not None
    assert verdict.get("success") is True
    assert verdict["result"]["post_write_recovery"] is True


def test_non_wrapper_unchanged(
    vault: Path, operations_dir: Path,
) -> None:
    """Regression guard: non-wrapper capabilities without their own
    manifest still hit the single-effect verify path. The new lookup
    must NOT trigger for arbitrary names."""
    note_uuid = "44444444-4444-4444-4444-444444444444"
    note_content = "non-wrapper body"
    note_path_rel, sha_hint = _make_note(vault, note_uuid, note_content)

    record = {
        "operation_id": "op_some_other_capability",
        "name": "some_unrelated_capability",
        "params": {"operation_id": "op_does_not_exist"},  # not a wrapper-id field
        "pwu_carrier": {
            "path": note_path_rel,
            "content_hint": sha_hint,
            "write_mode": "replace",
        },
    }

    verdict = RetrySweep(config={})._pre_verify_pwu(
        record["pwu_carrier"], record=record,
    )

    # Non-wrapper, no manifest → single-effect verify on carrier path →
    # note exists with right sha → "verified" → success.
    assert verdict is not None
    assert verdict.get("success") is True
