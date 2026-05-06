"""Threads tab — clustered modules with internal coupling.

* ``main`` — tab loader and ``window.threadsSurface``
* ``card`` — per-thread card rendering primitives
* ``actions`` — card action menus and handlers
* ``group`` — group-view rendering

Kept as a sub-package because card and group share rendering primitives
that aren't safe to split casually. See ``services/dashboard``
knowledge unit for the surface contract.
"""

from __future__ import annotations
