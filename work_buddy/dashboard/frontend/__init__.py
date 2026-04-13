"""Dashboard frontend — generates the single-page HTML app.

The page is built from inline modules (HTML, CSS, JS) so there's
no build step or static file serving.  Each concern lives in its
own submodule:

    frontend/
        styles.py               — CSS
        html.py                 — page structure
        script_main.py          — core tab JS (overview, tasks, etc.)
        script_workflows.py     — workflow view polling + tab management
        script_notifications.py — toasts + browser notifications
        script_triage.py        — triage clarify + review renderers
        script_threads.py       — thread chat component
        script_palette.py       — command palette

Adding a new tab:

1. Add a ``<button>`` to the tab bar in ``html.py`` ``_html()``
2. Add a ``<div class="tab-panel" id="...">`` in the panels section
3. Add a fetch+render function in ``script_main.py`` and call it
   from ``switchTab``
"""

from __future__ import annotations

from .html import _html
from .script_main import _script
from .script_notifications import _notification_script
from .script_palette import _command_palette_script
from .script_threads import _thread_chat_script
from .script_triage import _triage_clarify_script, _triage_review_script
from .script_workflows import _workflow_views_script
from .styles import _styles


def _vault_name() -> str:
    """Read the Obsidian vault name from config for obsidian:// URI links."""
    try:
        from pathlib import Path
        from work_buddy.config import load_config
        vault_root = load_config().get("vault_root", "")
        return Path(vault_root).name if vault_root else ""
    except Exception:
        return ""


def render_page() -> str:
    """Return the complete HTML page as a string."""
    vault = _vault_name()
    all_scripts = "\n".join([
        f"const WB_VAULT_NAME = {vault!r};",
        _script(),
        _workflow_views_script(),
        _notification_script(),
        _triage_clarify_script(),
        _triage_review_script(),
        _thread_chat_script(),
        _command_palette_script(),
    ])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>work-buddy dashboard</title>
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <style>{_styles()}</style>
</head>
<body>
    {_html()}
    <script>{all_scripts}</script>
</body>
</html>"""
