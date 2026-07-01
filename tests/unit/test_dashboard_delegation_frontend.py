"""Tests for the event-delegation dispatcher (core/delegation.py) and the
repo-wide "zero inline handlers" invariant.

FM-1 in the dashboard-frontend hardening: interactivity was wired through
inline ``onclick="fn('id')"`` attributes built by string concatenation, where
a quote in an interpolated arg silently truncated the handler at click time
(invisible to ``node --check`` and on page load). The whole frontend was
converted to a shared delegation dispatcher: renderers emit ``data-on-<event>``
attributes + escaped ``data-*`` args, and one document-level listener per event
type dispatches to registered ``window.wbAction`` handlers.

These tests pin:
1. the dispatcher's public API and page-LAST safety;
2. that the assembled page carries NO inline event-handler attributes (the
   gate that FM-1 previously lacked); and
3. a Node behaviour harness proving a quote-bearing arg round-trips through
   wbActAttrs -> attribute -> dataset -> handler without truncation.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

import pytest

from work_buddy.dashboard.frontend import render_page
from work_buddy.dashboard.frontend.scripts.core.delegation import script as _delegation_script
from work_buddy.dashboard.frontend.scripts.core.helpers import script as _helpers_script


# The DOM event-handler attributes we forbid inline. click/input/change/
# keydown/submit are the common bubbling handlers; the rest cover the special
# events the dispatcher also binds (contextmenu, mousedown, focusout, drag*).
_HANDLER_EVENTS = (
    "click|input|change|keydown|submit|contextmenu|mousedown|mouseup|"
    "blur|focus|dragstart|dragend|dragover|dragleave|dragenter|drop"
)
# Match the inline-ATTRIBUTE form only: on<event>= immediately followed by a
# quote. This excludes legitimate JS property assignments (el.onclick = fn),
# which are function references, not string-built attributes, and carry no
# FM-1 quoting risk.
_HANDLER_RE = re.compile(r"on(?:" + _HANDLER_EVENTS + r")\s*=\s*[\"']")


def test_dispatcher_defines_api():
    src = _delegation_script()
    assert "window.wbAction = function" in src
    assert "window.wbActAttrs = function" in src
    assert "window.wbAction('wbNoop'" in src


def test_dispatcher_no_module_scope_declarations():
    """Page-LAST ordering guard: everything lives inside the IIFE, so there are
    no column-0 let/const/var declarations that would sit in the TDZ for
    page-init code running after this module."""
    offenders = [
        line for line in _delegation_script().splitlines()
        if line[:1] not in (" ", "\t", "")
        and line.lstrip().split(" ")[0] in ("let", "const", "var")
    ]
    assert not offenders, f"module-scope declarations present: {offenders}"


def _strip_comments(text: str) -> str:
    """Remove block comments and full-line ``//`` comments. The only remaining
    ``on*=`` occurrences in the rendered page are doc comments that describe the
    former inline pattern; they always sit on their own ``//`` lines, so this is
    safe and does not touch ``//`` inside string literals (e.g. URLs)."""
    no_block = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(
        ln for ln in no_block.splitlines() if not ln.lstrip().startswith("//")
    )


def test_no_inline_handlers_in_rendered_page():
    """The gate FM-1 lacked: the fully-assembled page must carry zero inline
    event-handler attributes. Every interaction goes through delegation."""
    body = _strip_comments(render_page())
    hits = _HANDLER_RE.findall(body)
    assert not hits, f"{len(hits)} inline handler(s) remain in rendered page: {hits[:10]}"


def test_delegation_behavior_harness():
    """A quote-bearing arg round-trips through wbActAttrs -> attribute ->
    dataset -> handler without truncation (the FM-1 fix, proven at runtime)."""
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    harness = os.path.join(os.path.dirname(__file__), "eval_delegation_behavior.cjs")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", encoding="utf-8", delete=False
    ) as fh:
        fh.write(_helpers_script() + "\n" + _delegation_script())
        js_path = fh.name
    try:
        result = subprocess.run(
            ["node", harness, js_path], capture_output=True, text=True
        )
    finally:
        os.unlink(js_path)
    assert result.returncode == 0, (
        f"delegation behaviour harness failed:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
