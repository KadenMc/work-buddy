"""Tests for the single canonical HTML escaper (core/helpers.py).

FM-3 in the dashboard-frontend hardening: there used to be three escape
helpers (``escapeHtml`` defined twice via the ``textContent`` trick, plus a
threads-local ``_esc``), none of which escaped quotes. Quote-unsafe escaping
in attribute context is the same bug family as the inline-handler quoting
break. Stage 0 collapses the two global definitions into one that escapes
all five unsafe characters (``& < > " '``).

We verify:

1. ``core/helpers.py`` defines exactly one canonical escaper covering all
   five characters.
2. The two former per-module definitions (projects.py, workflows.py) are
   gone, so the assembled page declares ``escapeHtml`` exactly once.
3. A Node behavior harness proves the escaper escapes quotes at runtime —
   the property that makes it attribute-safe.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

from work_buddy.dashboard.frontend import assembled_js, render_page
from work_buddy.dashboard.frontend.scripts.core.helpers import script as _helpers_script
from work_buddy.dashboard.frontend.scripts.core.workflows import script as _workflows_script
from work_buddy.dashboard.frontend.scripts.tabs.projects import script as _projects_script


def test_helpers_defines_quote_safe_escaper():
    src = _helpers_script()
    assert "function escapeHtml(s)" in src
    # All five unsafe characters must be escaped — quotes are what make it
    # safe in attribute context.
    for frag in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert frag in src, f"escaper missing {frag}"
    # The old quote-unsafe textContent trick must NOT be how the canonical
    # escaper works.
    assert ".textContent = " not in src or "createElement('div')" not in src


def test_former_duplicate_definitions_are_gone():
    assert "function escapeHtml" not in _workflows_script()
    assert "function escapeHtml" not in _projects_script()


def test_assembled_page_declares_escaper_once():
    js = assembled_js()
    assert js.count("function escapeHtml") == 1


def test_escaper_escapes_all_five_chars_at_runtime():
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", encoding="utf-8", delete=False
    ) as fh:
        fh.write(_helpers_script())
        js_path = fh.name
    # Input:  a " b ' c < d > & e   (built via char codes to avoid quoting
    # hell in this driver string). Expected fully-escaped output.
    driver = (
        "const fs=require('fs'),vm=require('vm');"
        "const js=fs.readFileSync(process.argv[1],'utf-8');"
        "const s={console};vm.createContext(s);vm.runInContext(js,s);"
        "const inp=String.fromCharCode(97,34,98,39,99,60,100,62,38,101);"
        "const out=s.escapeHtml(inp);"
        "const exp='a&quot;b&#39;c&lt;d&gt;&amp;e';"
        "if(out!==exp){console.error('MISMATCH '+JSON.stringify(out));process.exit(1);}"
        "process.exit(0);"
    )
    try:
        result = subprocess.run(
            ["node", "-e", driver, js_path], capture_output=True, text=True
        )
    finally:
        os.unlink(js_path)
    assert result.returncode == 0, (
        f"escaper runtime check failed:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
