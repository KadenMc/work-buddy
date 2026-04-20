"""Tests for the inline command registry decorator."""

from __future__ import annotations

import pytest

from work_buddy.inline import registry


@pytest.fixture(autouse=True)
def _snapshot_registry():
    """Preserve / restore the module-level registry around each test."""
    snapshot = dict(registry._COMMANDS)  # noqa: SLF001
    try:
        yield
    finally:
        registry._COMMANDS.clear()  # noqa: SLF001
        registry._COMMANDS.update(snapshot)  # noqa: SLF001


def test_decorator_registers_metadata():
    registry.clear()

    @registry.inline_command(
        name="test/one",
        surfaces=["menu"],
        consume_mode="annotate",
        menu_label="One",
        description="unit test",
    )
    def handler(ctx):
        return {"ok": True}

    cmd = registry.get("test/one")
    assert cmd is not None
    assert cmd.name == "test/one"
    assert cmd.surfaces == ["menu"]
    assert cmd.consume_mode == "annotate"
    assert cmd.menu_label == "One"
    assert cmd.handler is handler


def test_decorator_idempotent_on_same_name():
    registry.clear()

    @registry.inline_command(name="test/dup", surfaces=["tag"])
    def h1(ctx):
        return 1

    @registry.inline_command(name="test/dup", surfaces=["menu"])
    def h2(ctx):
        return 2

    cmd = registry.get("test/dup")
    assert cmd.handler is h2
    assert cmd.surfaces == ["menu"]
    assert len(registry.list_commands()) == 1


def test_list_for_surface_filters():
    registry.clear()

    @registry.inline_command(name="a", surfaces=["menu"])
    def a(ctx):
        return None

    @registry.inline_command(name="b", surfaces=["tag"])
    def b(ctx):
        return None

    @registry.inline_command(name="c", surfaces=["menu", "tag"])
    def c(ctx):
        return None

    menu_names = {c.name for c in registry.list_for_surface("menu")}
    tag_names = {c.name for c in registry.list_for_surface("tag")}
    assert menu_names == {"a", "c"}
    assert tag_names == {"b", "c"}
