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

import hashlib
import html as _htmlstd

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


# WB_VAULT_NAME is per-render (config-derived), so it cannot live in the
# content-hashed cached bundle. The cached JS reads it from the
# <html data-vault-name> attribute that render_page emits; this bootstrap line
# is identical on every render, so it stays in the cacheable asset.
_VAULT_BOOTSTRAP = (
    "const WB_VAULT_NAME = (document.documentElement "
    "&& document.documentElement.dataset.vaultName) || '';\n"
)


def assembled_js() -> str:
    """The full concatenated frontend JavaScript — the single source of truth,
    used by render_page (via the hashed asset), the /assets route, and the
    frontend tests / init harness."""
    return _VAULT_BOOTSTRAP + "\n".join(fn() for fn in SCRIPTS)


def assembled_css() -> str:
    """The full concatenated frontend CSS."""
    return "\n".join([_styles()] + [fn() for fn in STYLES])


# Content-hashed asset cache. Built once, lazily. In --dev, Werkzeug reloads
# the whole process on any .py change (all frontend JS/CSS lives in .py
# modules), which re-imports this module and rebuilds the cache — so a static
# build-once is correct in both prod and dev.
_ASSETS: dict[str, tuple[bytes, str]] = {}
_JS_NAME: str = ""
_CSS_NAME: str = ""


def _build_assets() -> None:
    global _ASSETS, _JS_NAME, _CSS_NAME
    js = assembled_js().encode("utf-8")
    css = assembled_css().encode("utf-8")
    _JS_NAME = "app.%s.js" % hashlib.sha256(js).hexdigest()[:12]
    _CSS_NAME = "app.%s.css" % hashlib.sha256(css).hexdigest()[:12]
    _ASSETS = {
        _JS_NAME: (js, "application/javascript; charset=utf-8"),
        _CSS_NAME: (css, "text/css; charset=utf-8"),
    }


def _ensure_assets() -> None:
    if not _ASSETS:
        _build_assets()


def get_asset(filename: str) -> tuple[bytes, str] | None:
    """(bytes, content_type) for a hashed asset name, or None. Served by the
    dashboard's /assets/<filename> route with an immutable cache policy."""
    _ensure_assets()
    return _ASSETS.get(filename)


def render_page() -> str:
    """Return the complete HTML document.

    The ~800 KB of app JavaScript and the CSS are served as separate,
    content-hashed, immutably-cached assets (``/assets/app.<hash>.js|css``)
    rather than inlined, so reloads are 304s and a truncated asset transfer is
    a retryable fetch instead of a dead page. The document itself stays
    no-store (see the index route) so it always references the current hashed
    URLs.
    """
    _ensure_assets()
    vault_attr = _htmlstd.escape(_vault_name(), quote=True)
    return f"""<!DOCTYPE html>
<html lang="en" data-vault-name="{vault_attr}">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>work-buddy dashboard</title>
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <link rel="stylesheet" href="/assets/{_CSS_NAME}">
    <script src="/vendor/chart.umd.min.js"></script>
    <script src="/vendor/morphdom-umd.min.js"></script>
</head>
<body>
    {_html()}
    <script src="/assets/{_JS_NAME}"></script>
</body>
</html>"""
