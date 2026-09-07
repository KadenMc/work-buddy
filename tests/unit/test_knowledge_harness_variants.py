"""Harness-aware knowledge rendering, indexing, and manifest fallback contracts."""

from __future__ import annotations

import json
import importlib
from pathlib import Path

import pytest

from work_buddy import agent_session
from work_buddy.knowledge.editor import check_duplicate_placeholders
from work_buddy.knowledge.index import _build_doc
from work_buddy.knowledge.model import ConceptUnit, _resolve_placeholders
from work_buddy.knowledge.partition import _content_text
from work_buddy.knowledge.query import agent_docs
from work_buddy.knowledge.validate import _check_harness_placeholders, validate_store


def _unit(path, body):
    return ConceptUnit(path=path, name=path, description=path, content={"full": body})


@pytest.fixture
def store(monkeypatch, tmp_path):
    units = [
        _unit("guidance", "Read this recipe: <<wb:browser --harness>>"),
        _unit("browser", "Choose the browser surface."),
        _unit("browser/claudecode", "Use read_page for the pane."),
        _unit("browser/codexcli", "Use browser_snapshot for MCP."),
        _unit("browser/default", "Initialize the session and inspect available tools."),
    ]
    result = {unit.path: unit for unit in units}
    monkeypatch.setattr(importlib.import_module("work_buddy.knowledge.search"), "load_store", lambda **_: result)
    monkeypatch.setattr("work_buddy.knowledge.store.load_store", lambda **_: result)
    monkeypatch.setattr(agent_session, "get_agents_dir", lambda: tmp_path / "agents")
    monkeypatch.delenv("WORK_BUDDY_HARNESS_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    token = agent_session.set_originating_session(None)
    yield result
    agent_session.reset_originating_session(token)


def _manifest(tmp_path, session_id, payload, *, directory="session"):
    target = tmp_path / "agents" / f"{directory}_{session_id[:8]}" / "manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


@pytest.mark.parametrize(
    ("harness", "expected", "excluded"),
    [
        ("claudecode", "read_page", "browser_snapshot"),
        ("codexcli", "browser_snapshot", "read_page"),
        ("unknown", "Initialize the session", "browser_snapshot"),
        ("other", "Initialize the session", "read_page"),
    ],
)
def test_session_manifest_selects_recipe(store, tmp_path, monkeypatch, harness, expected, excluded):
    session_id = "session-one"
    _manifest(tmp_path, session_id, {"session_id": session_id, "harness_id": harness})
    monkeypatch.setenv("WORK_BUDDY_HARNESS_ID", "codexcli")
    token = agent_session.set_originating_session(session_id)
    try:
        rendered = agent_docs(path="guidance", depth="full")["unit"]["content"]
    finally:
        agent_session.reset_originating_session(token)
    assert expected in rendered
    assert excluded not in rendered


def test_uninitialized_session_uses_default_without_creating_manifest(store, tmp_path):
    assert "Initialize the session" in agent_docs(path="guidance", depth="full")["unit"]["content"]
    assert not list(tmp_path.rglob("manifest.json"))


def test_manifest_fallback_chain(store, tmp_path, monkeypatch):
    session_id = "session-one"
    manifest = _manifest(tmp_path, session_id, {"session_id": session_id})
    token = agent_session.set_originating_session(session_id)
    try:
        assert agent_session.get_originating_harness() == "unknown"
        monkeypatch.setenv("CODEX_THREAD_ID", "native-thread")
        assert agent_session.get_originating_harness() == "codexcli"
        monkeypatch.setenv("WORK_BUDDY_HARNESS_ID", "claudecode")
        assert agent_session.get_originating_harness() == "claudecode"
        manifest.write_text("broken JSON", encoding="utf-8")
        assert agent_session.get_originating_harness() == "claudecode"
        manifest.write_text("[]", encoding="utf-8")
        assert agent_session.get_originating_harness() == "claudecode"
        manifest.unlink()
        assert agent_session.get_originating_harness() == "claudecode"
        assert not manifest.exists()
    finally:
        agent_session.reset_originating_session(token)


def test_manifest_short_id_collision_does_not_select_another_caller(store, tmp_path):
    session_id = "identical-right"
    _manifest(tmp_path, session_id, {"session_id": "identical-wrong", "harness_id": "claudecode"})
    _manifest(tmp_path, session_id, {"session_id": session_id, "harness_id": "codexcli"}, directory="other")
    token = agent_session.set_originating_session(session_id)
    try:
        assert agent_session.get_originating_harness() == "codexcli"
    finally:
        agent_session.reset_originating_session(token)


@pytest.mark.parametrize("mode", ["lookup", "browse", "search", "ranked", "keyword"])
def test_explicit_harness_reaches_all_query_rendering_paths(store, monkeypatch, mode):
    monkeypatch.setenv("WORK_BUDDY_HARNESS_ID", "claudecode")
    kwargs = {"path": "guidance"}
    if mode == "browse":
        kwargs = {"scope": ""}
    elif mode == "search":
        kwargs = {"query": "guidance"}
    elif mode in ("ranked", "keyword"):
        kwargs = {"query": "recipe"}
        monkeypatch.setattr(
            importlib.import_module("work_buddy.knowledge.search"), "_search_via_consolidated",
            lambda *args, **kwargs: [{"path": "guidance", "score": 1.0}] if mode == "ranked" else [],
        )
        if mode == "keyword":
            store["guidance"].aliases = ["recipe"]
    result = agent_docs(**kwargs, depth="full", harness="codexcli")
    unit = result["unit"] if mode == "lookup" else next(u for u in result["results"] if u["path"] == "guidance")
    assert "browser_snapshot" in unit["content"]
    assert "read_page" not in unit["content"]


def test_missing_variant_uses_default_and_missing_default_is_validated(store, monkeypatch):
    del store["browser/codexcli"]
    assert "Initialize the session" in agent_docs(path="guidance", depth="full", harness="codexcli")["unit"]["content"]
    assert _check_harness_placeholders(store) == []
    del store["browser/default"]
    result = agent_docs(path="guidance", depth="full", harness="codexcli")
    assert "browser/default not found" in result["unit"]["content"]
    monkeypatch.setattr("work_buddy.knowledge.validate.load_store", lambda: store)
    validation = validate_store(checks=["harness_placeholder_default"])
    assert not validation["passed"]
    assert validation["errors"][0]["path"] == "guidance"


def test_recursive_none_preserves_harness_markup(store):
    rendered = agent_docs(path="guidance", depth="full", recursive="none", harness="codexcli")
    assert rendered["unit"]["content"] == store["guidance"].content["full"]


@pytest.mark.parametrize("recursive", ["default", "all"])
def test_transitive_harness_recipes_preserve_context_and_depth_limit(store, recursive):
    store["browser/codexcli"].content["full"] += " <<wb:nested --harness --recursive>>"
    store["nested"] = _unit("nested", "Nested recipe")
    store["nested/codexcli"] = _unit("nested/codexcli", "nested MCP instructions")
    store["nested/default"] = _unit("nested/default", "nested fallback instructions")
    store["guidance"].content["full"] = "<<wb:browser --harness --recursive>>"
    rendered = agent_docs(path="guidance", depth="full", recursive=recursive, harness="codexcli")
    assert "nested MCP instructions" in rendered["unit"]["content"]
    assert "fallback" not in rendered["unit"]["content"]
    capped = agent_docs(path="guidance", depth="full", recursive=recursive, max_depth=1, harness="codexcli")
    assert "truncated at depth 1" in capped["unit"]["content"]
    assert "nested MCP instructions" not in capped["unit"]["content"]


def test_plain_harness_placeholder_keeps_nested_markup(store):
    store["browser/codexcli"].content["full"] = "<<wb:other>>"
    assert "<<wb:other>>" in agent_docs(path="guidance", depth="full", harness="codexcli")["unit"]["content"]


def test_variant_dedup_uses_resolved_child_and_preserves_duplicate_rejection(store):
    text = "<<wb:browser --harness>> <<wb:browser/codexcli>>"
    rendered = _resolve_placeholders(text, store, harness="codexcli")
    assert rendered.count("Use browser_snapshot") == 1
    assert "browser/codexcli already expanded above" in rendered
    assert check_duplicate_placeholders("<<wb:browser --harness>> <<wb:browser>>")


def test_recursive_harness_cycle_is_bounded(store):
    store["browser/codexcli"].content["full"] = "cycle <<wb:browser --harness --recursive>>"
    rendered = agent_docs(path="guidance", depth="full", harness="codexcli", recursive="all")
    assert "browser/codexcli already expanded above" in rendered["unit"]["content"]


@pytest.mark.parametrize("builder", [_build_doc, _content_text])
def test_both_search_indexes_include_every_variant_independent_of_caller(store, monkeypatch, builder):
    store["browser/codexcli/nested"] = _unit("browser/codexcli/nested", "not a direct variant")
    monkeypatch.setattr(agent_session, "get_originating_harness", lambda: pytest.fail("index must not detect caller"))
    if builder is _build_doc:
        doc = builder("guidance", store["guidance"], store)
        texts = [doc.full_text, doc.content_text]
    else:
        texts = [builder(store["guidance"], store)]
    for text in texts:
        for variant, expected in [("claudecode", "read_page"), ("codexcli", "browser_snapshot"), ("default", "Initialize the session")]:
            assert f"<!-- wb: harness {variant} -->" in text
            assert expected in text
        assert "not a direct variant" not in text


def test_index_harness_variants_keep_transitive_resolution_and_dedup(store):
    store["shared"] = _unit("shared", "shared safety instruction")
    for variant in ("claudecode", "codexcli", "default"):
        store[f"browser/{variant}"].content["full"] += " <<wb:shared>>"
    store["guidance"].content["full"] = "<<wb:browser --harness --recursive>>"
    text = _build_doc("guidance", store["guidance"], store).content_text
    assert text.count("shared safety instruction") == 1
    assert text.count("shared already expanded above") == 2


def test_agent_docs_declaration_exposes_harness_keyword():
    from work_buddy.knowledge.file_store import read_unit

    declaration = read_unit(Path(__file__).resolve().parents[2] / "knowledge/store", "context/agent_docs")
    assert declaration["parameters"]["harness"]["type"] == "str"
    assert declaration["parameters"]["harness"]["required"] is False
