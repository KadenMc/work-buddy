"""Pull the watched value out of a fetched payload.

Three modes (the design's catalog): ``json_path`` (JSONPath over parsed JSON),
``css`` (CSS selector over HTML text), or ``hash`` (a content hash of the whole
body — the catch-all "did anything change"). Returns a JSON-serializable value;
the poller diffs ``content_hash(value)`` against the stored hash.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def content_hash(value: Any) -> str:
    """Stable SHA-256 of a JSON-serializable value (the diff key)."""
    raw = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def extract_value(mode: str, payload: Any, *, path: str | None = None) -> Any:
    """Extract the watched value. A single match returns the scalar; multiple
    matches return a list (so a collection watch diffs the whole set)."""
    if mode == "json_path":
        import jsonpath_ng

        matches = [m.value for m in jsonpath_ng.parse(path).find(payload)]
        return matches[0] if len(matches) == 1 else matches
    if mode == "css":
        from lxml import html as lxml_html

        text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        doc = lxml_html.fromstring(text)
        nodes = doc.cssselect(path)  # needs the `cssselect` package
        out = [n.text_content().strip() for n in nodes]
        return out[0] if len(out) == 1 else out
    if mode == "hash":
        raw = payload if isinstance(payload, (str, bytes)) else json.dumps(
            payload, sort_keys=True, default=str
        )
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return hashlib.sha256(raw).hexdigest()
    raise ValueError(f"unknown extract mode {mode!r}")
