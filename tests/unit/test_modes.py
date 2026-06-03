"""Tests for the mode-aware capability registry.

Covers: the mode registry (declarations + lookups + load-time validation),
``available_when`` gate resolution on capability/workflow declarations, the
session ``active_modes`` plumbing, the ``mode_toggle`` op, and the
``wb_search`` / ``wb_run`` mode-gating behavior.
"""

from __future__ import annotations

import pytest

from work_buddy.modes.registry import (
    ModeDef,
    _load_modes,
    get_known_mode_ids,
    get_mode_def,
)


def _write_mode(d, name: str, body: str) -> None:
    (d / f"{name}.yaml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Mode registry
# ---------------------------------------------------------------------------

class TestModeRegistry:
    def test_known_modes_include_dev_and_knowledge(self):
        assert {"dev", "knowledge"} <= get_known_mode_ids()

    def test_get_mode_def_returns_modedef(self):
        dev = get_mode_def("dev")
        assert isinstance(dev, ModeDef)
        assert dev.id == "dev"
        assert dev.activatable_when is None

    def test_get_mode_def_unknown_is_none(self):
        assert get_mode_def("nope_not_a_mode") is None

    def test_load_modes_parses_activatable_when(self, tmp_path):
        _write_mode(tmp_path, "exploration", "id: exploration\nlabel: Exploration\ndescription: x\n")
        _write_mode(tmp_path, "paper", "id: paper\nlabel: Paper\ndescription: x\nactivatable_when: '!exploration'\n")
        modes = _load_modes(tmp_path)
        assert set(modes) == {"exploration", "paper"}
        assert modes["paper"].activatable_when == "!exploration"

    def test_compound_activatable_when_against_known_ids(self, tmp_path):
        for m in ("dev", "knowledge", "admin"):
            _write_mode(tmp_path, m, f"id: {m}\nlabel: {m}\ndescription: x\n")
        _write_mode(tmp_path, "cothink", "id: cothink\nlabel: C\ndescription: x\nactivatable_when: '(dev & knowledge) | admin'\n")
        modes = _load_modes(tmp_path)
        assert modes["cothink"].activatable_when == "(dev & knowledge) | admin"

    def test_unknown_mode_in_activatable_when_fails(self, tmp_path):
        _write_mode(tmp_path, "paper", "id: paper\nlabel: Paper\ndescription: x\nactivatable_when: 'nonexistent'\n")
        with pytest.raises(ValueError):
            _load_modes(tmp_path)

    def test_bad_dsl_in_activatable_when_fails(self, tmp_path):
        _write_mode(tmp_path, "paper", "id: paper\nlabel: Paper\ndescription: x\nactivatable_when: 'a & & b'\n")
        with pytest.raises(ValueError):
            _load_modes(tmp_path)

    def test_invalid_mode_id_fails(self, tmp_path):
        _write_mode(tmp_path, "bad", "id: bad-id\nlabel: Bad\ndescription: x\n")
        with pytest.raises(ValueError):
            _load_modes(tmp_path)

    def test_missing_id_fails(self, tmp_path):
        _write_mode(tmp_path, "noid", "label: No ID\ndescription: x\n")
        with pytest.raises(ValueError):
            _load_modes(tmp_path)
