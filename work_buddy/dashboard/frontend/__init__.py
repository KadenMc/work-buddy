"""Dashboard frontend — generates the single-page HTML app.

The page is built from inline modules (HTML, CSS, JS) so there's
no build step or static file serving. Each concern lives in its
own submodule:

    frontend/
        styles.py            — global CSS
        html.py              — page structure
        scripts/             — JS modules organized by role
            core/            — page shell, event bus, helpers,
                               workflow polling, notifications,
                               command palette
            tabs/            — one module per panel in staticLoaders
                               (plus tabs/threads/* for the cluster)
            surfaces/        — workflow-view renderers + decorator
                               overlays (triage, resolution)

Adding a new tab:

1. Add a ``<button>`` to the tab bar in ``html.py`` ``_html()``
2. Add a ``<div class="tab-panel" id="...">`` in the panels section
3. Create ``scripts/tabs/<name>.py`` exposing ``script() -> str``
   (and optionally ``styles() -> str``)
4. Add the loader to ``staticLoaders`` in ``scripts/core/page.py``
5. Add the new module's ``script`` (and ``styles`` if applicable)
   to the ordered registry in ``scripts/__init__.py``
"""

from __future__ import annotations

from .html import _html
from .scripts import SCRIPTS, STYLES
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
    all_scripts = "\n".join(
        [f"const WB_VAULT_NAME = {vault!r};"]
        + [fn() for fn in SCRIPTS]
    )
    all_styles = "\n".join([_styles()] + [fn() for fn in STYLES])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>work-buddy dashboard</title>
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <style>{all_styles}</style>
    <script src="/vendor/chart.umd.min.js"></script>
    <script src="/vendor/morphdom-umd.min.js"></script>
</head>
<body>
    {_html()}
    <script>{all_scripts}</script>
</body>
</html>"""
