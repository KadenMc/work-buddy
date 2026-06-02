"""Unit tests for ``_resolve_input_map`` — the conductor's auto_run input wiring.

The load-bearing behavior under test: a workflow's ``params_schema`` declares
which caller params are optional, and the resolver must honor that — an absent
``__params__.<key>`` whose ``<key>`` is declared optional is **skipped** (so the
callable's own default applies), while any other missing source still errors.
This is what lets the natural dotted-wiring style (`input_map: {x: __params__.x}`)
work for optional params without breaking the bare/omitted call.
"""

from __future__ import annotations

from work_buddy.mcp_server.conductor import _resolve_input_map


class TestParamsSources:
    def test_whole_params_always_resolves_even_when_empty(self):
        kwargs, err = _resolve_input_map({"p": "__params__"}, {}, {})
        assert err is None
        assert kwargs == {"p": {}}

    def test_dotted_param_present_is_wired(self):
        kwargs, err = _resolve_input_map(
            {"target": "__params__.target"}, {}, {"target": "yesterday"},
        )
        assert err is None
        assert kwargs == {"target": "yesterday"}

    def test_absent_optional_param_is_skipped(self):
        # `target` declared optional + omitted → kwarg skipped (callable default
        # applies), NO error. This is the fix.
        kwargs, err = _resolve_input_map(
            {"target": "__params__.target"}, {}, {}, optional_params={"target"},
        )
        assert err is None
        assert kwargs == {}            # not wired → read_journal_state(target=None)

    def test_absent_param_not_declared_optional_errors(self):
        # Fail-loud preserved: a missing source that isn't declared optional
        # (a required key, or no schema info) still errors.
        kwargs, err = _resolve_input_map(
            {"target": "__params__.target"}, {}, {}, optional_params=set(),
        )
        assert kwargs == {}
        assert err is not None
        assert "__params__.target" in err

    def test_absent_unknown_key_errors_even_with_other_optionals(self):
        # A typo'd key isn't in optional_params → still errors (typo-safe).
        kwargs, err = _resolve_input_map(
            {"x": "__params__.tpyo"}, {}, {}, optional_params={"target"},
        )
        assert err is not None
        assert "tpyo" in err

    def test_nested_param_miss_is_not_skippable(self):
        # Only top-level __params__.<key> is skippable; nested a.b still errors
        # even if 'a' is named optional (the schema is flat).
        kwargs, err = _resolve_input_map(
            {"x": "__params__.a.b"}, {}, {"a": {}}, optional_params={"a"},
        )
        assert err is not None


class TestStepSources:
    def test_step_source_present_is_wired(self):
        kwargs, err = _resolve_input_map(
            {"cfg": "load-config"}, {"load-config": {"k": 1}}, {},
        )
        assert err is None
        assert kwargs == {"cfg": {"k": 1}}

    def test_missing_step_source_errors(self):
        kwargs, err = _resolve_input_map({"cfg": "load-config"}, {}, {})
        assert err is not None
        assert "load-config" in err

    def test_empty_input_map_is_noop(self):
        assert _resolve_input_map({}, {}, {}) == ({}, None)
