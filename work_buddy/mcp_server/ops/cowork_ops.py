"""Gateway registration shim for the Co-work document capabilities.

The ``cowork_doc_*`` ops are authored in ``work_buddy.cowork.ops`` (their home
alongside the rest of the surface), not here. ``load_builtin_ops`` only imports
modules inside this package, so this thin shim exists to pull that registration
into the builtin-op load path. It mirrors the ``truth_ops`` idiom: import the
registration entry point and call it at import time.

The module name ``cowork_ops`` matches the ``cowork`` capability category, so
the loader's safe-degradation convention can pair a declaration with its op
module when an optional dependency is absent.
"""

from __future__ import annotations

from work_buddy.cowork.ops import register_ops

register_ops()


__all__ = ["register_ops"]
