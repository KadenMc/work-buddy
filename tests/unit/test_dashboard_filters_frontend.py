"""Tests for the shared chip-filter widget (core/filters.py) and the five
filter sites that adopt it.

The widget JS lives in a Python string assembled into the single-page app.
We verify three things:

1. The module exposes ``window.wbRenderFilters``, covers all three modes,
   emits the canonical ARIA/markup, and obeys the page-LAST ordering and
   read-only invariants (string-content asserts).
2. Selection is caller-owned — a Node behavior harness proves the widget
   re-derives active chips from ``getSelected`` and never reads them back
   from the DOM (the morphdom-survival property).
3. Each of the five filter sites now mounts the widget and its bespoke
   handlers/classes are gone.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from work_buddy.dashboard.frontend import assembled_css, assembled_js, render_page
from work_buddy.dashboard.frontend.scripts import SCRIPTS, STYLES
from work_buddy.dashboard.frontend.scripts.core.filters import script as _filters_script
from work_buddy.dashboard.frontend.scripts.core.filters import styles as _filters_styles
from work_buddy.dashboard.frontend.scripts.core import filters as _filters_mod
from work_buddy.dashboard.frontend.scripts.core import helpers as _helpers_mod
from work_buddy.dashboard.frontend.scripts.core import page as _page_mod
from work_buddy.dashboard.frontend.scripts.tabs.chats import script as _chats_script
from work_buddy.dashboard.frontend.scripts.tabs.costs import script as _costs_script
from work_buddy.dashboard.frontend.scripts.tabs.inference import script as _inference_script
from work_buddy.dashboard.frontend.scripts.tabs.inference import styles as _inference_styles
from work_buddy.dashboard.frontend.scripts.tabs.tasks import script as _tasks_script


# --------------------------------------------------------------------------
# Widget module — string-content invariants
# --------------------------------------------------------------------------

def test_widget_defines_entry_point():
    src = _filters_script()
    assert "window.wbRenderFilters = function" in src
    assert "window._wbFilterConfigs" in src


def test_widget_covers_three_modes():
    src = _filters_script()
    # single (radiogroup), multi (toggle Set), grouped (family tristate).
    assert "'single'" in src or "=== 'single'" in src
    assert "_wbFiltersFlatMarkup" in src
    assert "_wbFiltersGroupedMarkup" in src
    # grouped tristate derivation.
    assert "_wbFilterFamilyState" in src
    assert "is-indeterminate" in src


def test_widget_modifier_solo_dispatch():
    """The solo affordance is centralized: chip/member/family clicks all check
    Alt/Shift before toggling, replacing the per-tab costsModel* pairs."""
    src = _filters_script()
    assert "altKey" in src and "shiftKey" in src
    assert "_wbFilterFamilyClick" in src and "_wbFilterMemberClick" in src


def test_widget_emits_aria_and_keyboard():
    src = _filters_script()
    assert 'role="radio"' in src or "role: role" in src or "'radio'" in src
    assert "aria-checked" in src
    assert "aria-pressed" in src
    assert "'mixed'" in src  # tristate family -> aria-pressed="mixed" at runtime
    # Keyboard nav is additive but part of the canonical behavior.
    assert "_wbFilterKey" in src
    assert "ArrowRight" in src and "ArrowLeft" in src


def test_widget_has_no_module_scope_declarations():
    """Page-LAST ordering guard: the module must not declare any module-scope
    let/const/var (which would sit in the TDZ for page-init code that runs
    after it). Only window.* assignments + hoisted function declarations are
    allowed at column 0."""
    src = _filters_script()
    offenders = [
        line for line in src.splitlines()
        if line[:1] not in (" ", "\t", "") and line.lstrip().split(" ")[0] in ("let", "const", "var")
    ]
    assert not offenders, f"module-scope declarations present: {offenders}"


def test_widget_performs_no_fetch():
    """Read-only / view-only: the widget never fetches. It only invokes the
    caller's onChange; the caller decides whether a change needs a refetch."""
    assert "fetch(" not in _filters_script()


def test_widget_styles_define_canonical_classes():
    css = _filters_styles()
    for cls in (
        ".wb-filter-chip",
        ".wb-filter-chip.is-active",
        ".wb-filters.is-segmented",
        ".wb-filter-family-pill.is-indeterminate",
        ".wb-filter-reset",
        ".wb-filter-chip:focus-visible",
    ):
        assert cls in css, f"missing canonical class {cls}"


# --------------------------------------------------------------------------
# Registration / ordering
# --------------------------------------------------------------------------

def test_registered_in_order():
    """filters.script must sit after helpers (it uses escapeHtml) and before
    page (which runs init at load); styles registered too."""
    assert _filters_mod.script in SCRIPTS
    assert _filters_mod.styles in STYLES
    assert SCRIPTS.index(_helpers_mod.script) < SCRIPTS.index(_filters_mod.script)
    assert SCRIPTS.index(_filters_mod.script) < SCRIPTS.index(_page_mod.script)


def test_widget_in_assembled_page():
    # JS lives in the assembled bundle, CSS in the assembled stylesheet (both
    # now served as external assets rather than inlined in the page).
    assert "window.wbRenderFilters = function" in assembled_js()
    assert ".wb-filter-chip" in assembled_css()


# --------------------------------------------------------------------------
# Behavior harness — selection survives re-render (morphdom property)
# --------------------------------------------------------------------------

def test_selection_survives_rerender_and_tristate():
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    harness = (Path(__file__).parent / "eval_filters_behavior.cjs").resolve()
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", encoding="utf-8", delete=False
    ) as fh:
        fh.write(_filters_script())
        js_path = fh.name
    try:
        result = subprocess.run(
            ["node", str(harness), js_path], capture_output=True, text=True
        )
    finally:
        import os
        os.unlink(js_path)
    assert result.returncode == 0, (
        f"filter behavior harness failed:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


# --------------------------------------------------------------------------
# Per-site: each filter site mounts the widget; bespoke handlers/classes gone
# --------------------------------------------------------------------------

def test_inference_uses_widget():
    src = _inference_script()
    assert "wbRenderFilters('inf-filters'" in src
    # Old hand-rolled toggle dispatchers + chip helper are gone.
    assert "_infFilterChip" not in src
    assert "_infToggleWhere" not in src and "_infToggleStatus" not in src
    # Tab-local filter CSS retired.
    assert ".inf-filters" not in _inference_styles()


def test_tasks_uses_widget():
    src = _tasks_script()
    assert "wbRenderFilters('task-state-chips'" in src
    # Selection still caller-owned + feeds the task view.
    assert "_taskOnStateChange" in src and "_refreshTaskView" in src
    # Old per-chip createElement loop / class gone (the container id
    # 'task-state-chips' remains; the retired class literal must not).
    assert "'task-state-chip'" not in src


def test_chats_advanced_uses_widget():
    src = _chats_script()
    assert "wbRenderFilters('chats-advanced-filters'" in src
    assert "_chatsOnAdvChange" in src
    # Old toggle/visual handlers gone.
    assert "chatsToggleFilter" not in src
    assert "chatsUpdatePillVisuals" not in src


def test_chats_rail_uses_widget():
    src = _chats_script()
    assert "wbRenderFilters('chats-rail-selector'" in src
    # Old .costs-pill-based rail pill markup gone.
    assert "chats-rail-pill" not in src


def test_costs_activity_and_models_use_widget():
    src = _costs_script()
    assert "wbRenderFilters('costs-activity-filter'" in src
    assert "wbRenderFilters('costs-models-filter'" in src
    # Bespoke costs filter handlers gone (the widget centralizes them).
    assert "costsModelClick" not in src
    assert "costsModelFamilyToggle" not in src
    assert "costsModelsReset" not in src
    assert "_costsSyncActivityPills" not in src
    # Selection state still caller-owned + still reaches /api/costs.
    assert "_costsOnModelChange" in src
    assert "costsState.selectedModels" in src


def test_old_filter_css_clusters_removed():
    page = assembled_css()
    for retired in (
        ".costs-pill",
        ".costs-filter-pill",
        ".costs-family-pill",
        ".costs-models-reset",
        ".task-state-chip",
        ".chats-filter-pill",
        ".inf-filters ",
    ):
        assert retired not in page, f"retired CSS class still present: {retired}"
