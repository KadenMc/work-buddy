"""Tests for work_buddy.triage.card_actions.build_card_actions().

Coverage:
  - happy path: email_message descriptor → one well-formed action
  - other sources without open_action → empty list
  - unknown source → empty list
  - param-map dot-path resolution (top-level + nested + missing keys)
  - defensive guards: malformed descriptor shapes, empty values, etc.
  - integration via load_source_registry (the email_message default
    descriptor produces the expected action dict)
"""

from __future__ import annotations

from work_buddy.triage.card_actions import _resolve_path, build_card_actions
from work_buddy.triage.sources import (
    SourceDescriptor,
    load_source_registry,
    reset_for_tests,
)


# ---------------------------------------------------------------------------
# _resolve_path
# ---------------------------------------------------------------------------


def test_resolve_path_top_level():
    assert _resolve_path({"label": "Hello"}, "label") == "Hello"


def test_resolve_path_nested_metadata():
    item = {"metadata": {"provider_message_id": "abc@host"}}
    assert _resolve_path(item, "metadata.provider_message_id") == "abc@host"


def test_resolve_path_missing_key_returns_none():
    assert _resolve_path({}, "metadata.x") is None
    assert _resolve_path({"metadata": {}}, "metadata.x") is None


def test_resolve_path_traverses_into_non_dict_returns_none():
    """Ensures we don't crash when a parent in the path is a string/int/list."""
    item = {"metadata": "not-a-dict"}
    assert _resolve_path(item, "metadata.x") is None


def test_resolve_path_empty_returns_none():
    assert _resolve_path({"a": 1}, "") is None


# ---------------------------------------------------------------------------
# build_card_actions — happy path
# ---------------------------------------------------------------------------


def _email_descriptor():
    """The shipped email_message descriptor — pulls through the real
    config block so the test catches drift between sources.py and the
    helper."""
    reset_for_tests()
    return load_source_registry()["email_message"]


def test_build_card_actions_email_happy_path():
    desc = _email_descriptor()
    item = {
        "id": "email_abc",
        "label": "Subject",
        "metadata": {
            "provider_message_id": "abc@host",
            "folder_path": "imap://acct1/INBOX",
        },
    }
    actions = build_card_actions("email_message", item, descriptor=desc)
    assert len(actions) == 1
    a = actions[0]
    assert a["label"] == "Open in Thunderbird"
    assert a["command_id"] == "work-buddy::email_display"
    assert a["params"] == {
        "provider_message_id": "abc@host",
        "folder_path": "imap://acct1/INBOX",
    }


def test_build_card_actions_default_lookup():
    """Caller doesn't pass descriptor → helper falls back to
    get_descriptor(). Covers the runtime path in the presentation
    builder (which omits the descriptor for brevity)."""
    item = {
        "id": "email_abc",
        "metadata": {
            "provider_message_id": "abc@host",
            "folder_path": "imap://acct1/INBOX",
        },
    }
    actions = build_card_actions("email_message", item)
    assert len(actions) == 1
    assert actions[0]["command_id"] == "work-buddy::email_display"


# ---------------------------------------------------------------------------
# build_card_actions — sources without an open_action
# ---------------------------------------------------------------------------


def test_build_card_actions_no_open_action_returns_empty():
    """Sources without an open_action config return []. Existing
    chrome_tab / journal_thread / inline cards must continue to render
    unchanged (their items will simply have no `actions` key)."""
    for source in ("chrome_tab", "journal_thread", "inline"):
        actions = build_card_actions(source, {"id": "x", "metadata": {}})
        assert actions == [], source


def test_build_card_actions_unknown_source_returns_empty():
    actions = build_card_actions("totally_made_up_source", {"id": "x"})
    assert actions == []


# ---------------------------------------------------------------------------
# build_card_actions — defensive guards
# ---------------------------------------------------------------------------


def _desc_with(open_action):
    return SourceDescriptor(
        name="custom_source",
        ttl_days=None,
        quarantine_triggers=[],
        config={"open_action": open_action} if open_action is not None else {},
    )


def test_build_card_actions_missing_param_drops_action():
    """If the param_map references a metadata key that isn't on the
    item, the entire action is dropped — better an absent button than
    a broken click."""
    desc = _desc_with({
        "label": "Open",
        "capability": "fake_cap",
        "param_map": {"id": "metadata.required_id"},
    })
    item = {"id": "x", "metadata": {}}  # no required_id
    actions = build_card_actions("custom_source", item, descriptor=desc)
    assert actions == []


def test_build_card_actions_empty_string_param_drops_action():
    """An empty-string value is treated the same as missing — empty
    folder_path would be a broken handle."""
    desc = _desc_with({
        "label": "Open",
        "capability": "fake_cap",
        "param_map": {"folder_path": "metadata.folder_path"},
    })
    item = {"id": "x", "metadata": {"folder_path": ""}}
    actions = build_card_actions("custom_source", item, descriptor=desc)
    assert actions == []


def test_build_card_actions_missing_capability_drops():
    desc = _desc_with({"label": "Open", "param_map": {}})  # no capability
    actions = build_card_actions("custom_source", {"id": "x"}, descriptor=desc)
    assert actions == []


def test_build_card_actions_missing_label_drops():
    desc = _desc_with({"capability": "fake_cap", "param_map": {}})  # no label
    actions = build_card_actions("custom_source", {"id": "x"}, descriptor=desc)
    assert actions == []


def test_build_card_actions_non_dict_open_action_drops():
    desc = _desc_with("not-a-dict")
    actions = build_card_actions("custom_source", {"id": "x"}, descriptor=desc)
    assert actions == []


def test_build_card_actions_non_dict_param_map_drops():
    desc = _desc_with({"label": "Open", "capability": "fake_cap", "param_map": "wat"})
    actions = build_card_actions("custom_source", {"id": "x"}, descriptor=desc)
    assert actions == []


def test_build_card_actions_no_param_map_emits_action_with_empty_params():
    """A capability that takes no params (e.g. ``email_health``) should
    still get a working action."""
    desc = _desc_with({
        "label": "Health Check",
        "capability": "email_health",
        # no param_map at all
    })
    actions = build_card_actions("custom_source", {"id": "x"}, descriptor=desc)
    assert len(actions) == 1
    assert actions[0]["params"] == {}


def test_build_card_actions_empty_inputs_safe():
    assert build_card_actions("", {}) == []
    assert build_card_actions("email_message", None) == []  # type: ignore[arg-type]


def test_build_card_actions_param_map_silently_skips_non_string_keys():
    """Defensive: a malformed YAML where keys/values aren't strings
    just gets skipped, not crashed."""
    desc = _desc_with({
        "label": "Open",
        "capability": "fake_cap",
        "param_map": {
            "good": "metadata.x",
            123: "metadata.y",            # non-string key — skip
            "bad": ["metadata.z"],         # non-string value — skip
        },
    })
    item = {"metadata": {"x": "ok", "y": "yy", "z": "zz"}}
    actions = build_card_actions("custom_source", item, descriptor=desc)
    assert len(actions) == 1
    assert actions[0]["params"] == {"good": "ok"}
