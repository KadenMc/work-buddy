"""Shared dashboard frontend utilities.

Originally home of the Slice-4 surfaces (Review Queue / Daily
Log / Engage). Those views were removed once the Threads tab became
the canonical resolution surface. What remains here are two small
utilities still used by other modules:

- ``_autEsc`` — the HTML-escape helper used by ``tabs/today.py``.
- ``.section-subtitle`` — the small grey caption next to section
  titles, used by Today and the (kept) Review Queue / inline
  controls.

Kept named ``tabs/automation.*`` to minimize churn in
``frontend/__init__.py`` and the rendered ``<script>`` order. If a
future cleanup pass moves these utilities into ``styles.py`` and
``tabs/today.py``, this module can be deleted.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Shared frontend helpers ------------------------------------------

function _autEsc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
}
"""


def styles() -> str:
    return r"""
/* Section-title caption — small grey suffix used by Today and other
 * tabs that want a "<title> · subtitle" header. Was originally
 * defined alongside the Slice 4 surfaces; preserved here because the
 * remaining tabs (Today, Review) still consume it.
 */
.section-subtitle {
    font-size: 11px;
    font-weight: normal;
    color: var(--text-muted, #888);
    margin-left: 8px;
}
"""
