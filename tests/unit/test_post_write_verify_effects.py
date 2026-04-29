"""Fix (b) regression tests: effect-graph-aware post-write verification.

Background — `t-e2f1a8c4`: when a multi-effect capability (e.g.
`task_create` writes a note file AND a master-list line) hits PWU on
the first effect, the single-effect verifier reads the path on the
PWU exception, sees the first effect landed, and declares "verified".
But the second effect was never attempted. The verifier didn't know
about it.

Fix: capabilities can declare an `effects` manifest. The new
`verify_post_write_effects(effects, params)` walks all declared
effects and returns a verdict:

- "verified"      — all effects present
- "partial"       — some present, some not
- "absent"        — none present
- "indeterminate" — couldn't determine
"""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.config import load_config
from work_buddy.obsidian.effects import EffectSpec
from work_buddy.obsidian.post_write_verify import (
    _aggregate_effect_verdicts,
    _verify_one_effect,
    verify_post_write_effects,
)


@pytest.fixture
def vault(tmp_path: Path, monkeypatch) -> Path:
    """Point vault_root at a temp dir for filesystem-only verify."""
    cfg = dict(load_config())
    cfg["vault_root"] = str(tmp_path)
    monkeypatch.setattr(
        "work_buddy.obsidian.post_write_verify.load_config",
        lambda: cfg,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Aggregation logic
# ---------------------------------------------------------------------------


def test_aggregate_all_verified():
    assert _aggregate_effect_verdicts(["verified", "verified"]) == "verified"


def test_aggregate_partial():
    assert _aggregate_effect_verdicts(["verified", "absent"]) == "partial"
    assert _aggregate_effect_verdicts(
        ["absent", "verified", "absent"]
    ) == "partial"


def test_aggregate_all_absent():
    assert _aggregate_effect_verdicts(["absent", "absent"]) == "absent"


def test_aggregate_all_indeterminate():
    assert _aggregate_effect_verdicts(
        ["indeterminate", "indeterminate"]
    ) == "indeterminate"


def test_aggregate_indeterminate_with_absent_is_absent():
    """Conservative: if any effect is conclusively absent, the verdict
    is absent (schedule a retry) rather than indeterminate."""
    assert _aggregate_effect_verdicts(
        ["indeterminate", "absent"]
    ) == "absent"


def test_aggregate_empty_list_is_indeterminate():
    """Defensive: empty manifest is treated as indeterminate so the
    caller falls back to single-effect behavior."""
    assert _aggregate_effect_verdicts([]) == "indeterminate"


# ---------------------------------------------------------------------------
# Single-effect verifier function
# ---------------------------------------------------------------------------


def test_verify_one_substring_verified(vault):
    p = vault / "test.md"
    p.write_text("hello world", encoding="utf-8")
    assert _verify_one_effect(
        path="test.md", witness="world", mode="substring",
    ) == "verified"


def test_verify_one_substring_absent(vault):
    p = vault / "test.md"
    p.write_text("hello", encoding="utf-8")
    assert _verify_one_effect(
        path="test.md", witness="world", mode="substring",
    ) == "absent"


def test_verify_one_absent_mode_witness_present(vault):
    """absent-mode: witness PRESENT means delete didn't land → 'absent'."""
    p = vault / "test.md"
    p.write_text("still has the marker", encoding="utf-8")
    assert _verify_one_effect(
        path="test.md", witness="marker", mode="absent",
    ) == "absent"


def test_verify_one_absent_mode_witness_missing(vault):
    """absent-mode: witness MISSING means delete landed → 'verified'."""
    p = vault / "test.md"
    p.write_text("clean", encoding="utf-8")
    assert _verify_one_effect(
        path="test.md", witness="deleted", mode="absent",
    ) == "verified"


def test_verify_one_absent_mode_file_missing(vault):
    """absent-mode: missing file is the strongest 'absent' — verified."""
    assert _verify_one_effect(
        path="never_existed.md", witness="anything", mode="absent",
    ) == "verified"


def test_verify_one_substring_file_missing(vault):
    """For non-absent modes, missing file means the write didn't land."""
    assert _verify_one_effect(
        path="never_existed.md", witness="anything", mode="substring",
    ) == "absent"


def test_verify_one_no_witness_just_existence(vault):
    """When no witness is provided, file existence is the proof."""
    p = vault / "test.md"
    p.write_text("any content", encoding="utf-8")
    assert _verify_one_effect(
        path="test.md", witness=None, mode="substring",
    ) == "verified"


# ---------------------------------------------------------------------------
# verify_post_write_effects (the integration surface)
# ---------------------------------------------------------------------------


def _no_resolver(_params):
    """Sentinel resolver returning empty dict (no generated values needed)."""
    return {}


def test_all_effects_verified(vault):
    (vault / "tasks").mkdir()
    (vault / "tasks" / "notes").mkdir()
    (vault / "tasks" / "notes" / "abc.md").write_text(
        "# my task\n\n## summary\nbody", encoding="utf-8",
    )
    (vault / "tasks" / "master-task-list.md").write_text(
        "- [ ] #todo my task 🆔 t-12345678\n", encoding="utf-8",
    )
    effects = [
        EffectSpec(
            kind="file_write",
            path_template="tasks/notes/{note_uuid}.md",
            witness_template="{task_text}",
            witness_mode="substring",
            resolver=lambda p: {"task_id": "t-12345678", "note_uuid": "abc"},
        ),
        EffectSpec(
            kind="line_append",
            path="tasks/master-task-list.md",
            witness_template="🆔 {task_id}",
            witness_mode="substring",
            resolver=lambda p: {"task_id": "t-12345678", "note_uuid": "abc"},
        ),
    ]
    params = {"task_text": "my task", "summary": "body"}
    assert verify_post_write_effects(effects, params=params) == "verified"


def test_partial_first_effect_landed_second_missing(vault):
    """The marquee case: PWU on note write; note landed; master-list line
    never written. Single-effect verify would say 'verified' on the note;
    multi-effect detects the missing line."""
    (vault / "tasks" / "notes").mkdir(parents=True)
    (vault / "tasks" / "notes" / "abc.md").write_text(
        "# my task\n", encoding="utf-8",
    )
    # Master-list file exists but doesn't contain our task_id.
    (vault / "tasks").mkdir(exist_ok=True)
    (vault / "tasks" / "master-task-list.md").write_text(
        "- [ ] #todo unrelated 🆔 t-aabbccdd\n",
        encoding="utf-8",
    )

    effects = [
        EffectSpec(
            kind="file_write",
            path_template="tasks/notes/{note_uuid}.md",
            witness_template="{task_text}",
            resolver=lambda p: {"task_id": "t-12345678", "note_uuid": "abc"},
        ),
        EffectSpec(
            kind="line_append",
            path="tasks/master-task-list.md",
            witness_template="🆔 {task_id}",
            resolver=lambda p: {"task_id": "t-12345678", "note_uuid": "abc"},
        ),
    ]
    params = {"task_text": "my task"}
    verdict = verify_post_write_effects(effects, params=params)
    assert verdict == "partial", (
        f"Expected partial (first effect verified, second absent); got {verdict}"
    )


def test_no_effects_landed(vault):
    (vault / "tasks").mkdir()
    (vault / "tasks" / "master-task-list.md").write_text("", encoding="utf-8")
    # No note file.

    effects = [
        EffectSpec(
            kind="file_write",
            path_template="tasks/notes/{note_uuid}.md",
            witness_template="{task_text}",
            resolver=lambda p: {"task_id": "t-99", "note_uuid": "xyz"},
        ),
        EffectSpec(
            kind="line_append",
            path="tasks/master-task-list.md",
            witness_template="🆔 {task_id}",
            resolver=lambda p: {"task_id": "t-99", "note_uuid": "xyz"},
        ),
    ]
    assert verify_post_write_effects(
        effects, params={"task_text": "x"},
    ) == "absent"


def test_resolver_returns_none_marks_indeterminate(vault):
    """When the resolver can't resolve generated values (e.g. cache
    miss), that effect is indeterminate. Aggregation flips to absent
    if any other effect is conclusively absent (conservative)."""
    effects = [
        EffectSpec(
            kind="file_write",
            path_template="tasks/notes/{note_uuid}.md",
            witness_template="{task_text}",
            resolver=lambda p: None,  # cache miss
        ),
    ]
    assert verify_post_write_effects(
        effects, params={"task_text": "x"},
    ) == "indeterminate"


def test_resolver_partial_values_marks_indeterminate(vault):
    """A resolver returning a dict with any None value is treated as
    'couldn't fully resolve' and that effect goes indeterminate."""
    effects = [
        EffectSpec(
            kind="line_append",
            path="tasks/master-task-list.md",
            witness_template="🆔 {task_id}",
            resolver=lambda p: {"task_id": None},
        ),
    ]
    assert verify_post_write_effects(
        effects, params={},
    ) == "indeterminate"


def test_no_resolver_uses_params_only(vault):
    """An effect without a resolver still works — templates resolve
    against params alone."""
    (vault / "tasks").mkdir()
    (vault / "tasks" / "master-task-list.md").write_text(
        "🆔 t-deadbeef\n", encoding="utf-8",
    )

    effects = [
        EffectSpec(
            kind="line_append",
            path="tasks/master-task-list.md",
            witness_template="🆔 {task_id}",
        ),
    ]
    assert verify_post_write_effects(
        effects, params={"task_id": "t-deadbeef"},
    ) == "verified"


def test_empty_effects_list_is_indeterminate():
    """Defensive: empty manifest → indeterminate (caller falls back)."""
    assert verify_post_write_effects([], params={}) == "indeterminate"


# ---------------------------------------------------------------------------
# task_create's actual resolver
# ---------------------------------------------------------------------------


def test_task_create_effects_resolver_cache_hit(tmp_path, monkeypatch):
    """The resolver pulls task_id and note_uuid from the C.2 cache."""
    from work_buddy.obsidian.tasks import mutations
    from work_buddy.obsidian.tasks.mutations import (
        _create_task_idempotency_key,
        _record_idempotent_create_ids,
        create_task_effects_resolver,
    )

    # Point the cache at tmp_path
    monkeypatch.setattr(
        mutations, "_idempotency_dir",
        lambda: tmp_path / "create_task_idempotency",
    )

    params = {
        "task_text": "test task",
        "summary": "summary text",
        "project": "wb",
        "urgency": "high",
        "contract": None,
        "tags": ["paper/x"],
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
    _record_idempotent_create_ids(key, "t-feedface", "abcdef-12345")

    out = create_task_effects_resolver(params)
    assert out == {"task_id": "t-feedface", "note_uuid": "abcdef-12345"}


def test_task_create_effects_resolver_cache_miss(tmp_path, monkeypatch):
    from work_buddy.obsidian.tasks import mutations
    from work_buddy.obsidian.tasks.mutations import create_task_effects_resolver

    monkeypatch.setattr(
        mutations, "_idempotency_dir",
        lambda: tmp_path / "create_task_idempotency",
    )

    out = create_task_effects_resolver({
        "task_text": "never seen", "summary": "x",
        "project": None, "urgency": "medium",
        "contract": None, "tags": None, "due_date": None,
    })
    assert out is None  # cache miss → resolver returns None


def test_task_create_effects_resolver_no_summary_no_note_uuid(
    tmp_path, monkeypatch,
):
    """A task created without summary has no note_uuid in the cache;
    resolver returns task_id only (no note_uuid key)."""
    from work_buddy.obsidian.tasks import mutations
    from work_buddy.obsidian.tasks.mutations import (
        _create_task_idempotency_key,
        _record_idempotent_create_ids,
        create_task_effects_resolver,
    )

    monkeypatch.setattr(
        mutations, "_idempotency_dir",
        lambda: tmp_path / "create_task_idempotency",
    )

    params = {
        "task_text": "no-note task",
        "summary": None,
        "project": None,
        "urgency": "medium",
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
        tags=[],
        due_date=params["due_date"],
    )
    _record_idempotent_create_ids(key, "t-nonotenote", None)

    out = create_task_effects_resolver(params)
    assert out == {"task_id": "t-nonotenote"}
