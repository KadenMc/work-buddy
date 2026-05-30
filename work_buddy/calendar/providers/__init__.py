"""Concrete calendar providers.

Selected by :func:`work_buddy.calendar.provider.get_calendar_provider` from the
``calendar.provider`` config key. ``obsidian_bridge`` is the transitional
bootstrap adapter (wraps the eval_js path in ``work_buddy.calendar.env``);
``fake`` is the in-memory provider for tests. A future ``google_native``
adapter (own-OAuth) arrives in a later PR and retires the bridge at read+write
parity (DECISIONS D4).
"""
