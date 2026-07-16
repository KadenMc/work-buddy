"""Web Annotation text selectors and safe quote re-anchoring.

The exact-first and whitespace-tolerant quote search is adapted from
``aov/findings/anchor.py`` in the GPL-3.0 agentic-output-verification project.
This port keeps AOV's hallucination firewall while adding Web Annotation
quote context, position selectors, and an immutable snapshot hash guard.
"""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from work_buddy.truth.contracts import AnchorError
from work_buddy.truth.identity import sha256_text


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ResolvedAnchor:
    """A uniquely resolved text span using Unicode code-point offsets."""

    start: int
    end: int
    exact: str


@dataclass(frozen=True, slots=True)
class CompositeSelector:
    """A Web Annotation quote selector paired with an optional position selector.

    ``start`` and ``end`` use Unicode code-point offsets, matching Python string
    indexes and the Web Annotation TextPositionSelector contract.
    """

    exact: str
    prefix: str = ""
    suffix: str = ""
    start: int | None = None
    end: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.exact, str) or not self.exact.strip():
            raise AnchorError("TextQuoteSelector exact must be a non-empty string")
        if not isinstance(self.prefix, str):
            raise AnchorError("TextQuoteSelector prefix must be a string")
        if not isinstance(self.suffix, str):
            raise AnchorError("TextQuoteSelector suffix must be a string")
        if (self.start is None) != (self.end is None):
            raise AnchorError(
                "TextPositionSelector requires both start and end or neither"
            )
        if self.start is None:
            return
        if (
            isinstance(self.start, bool)
            or isinstance(self.end, bool)
            or not isinstance(self.start, int)
            or not isinstance(self.end, int)
        ):
            raise AnchorError(
                "TextPositionSelector start and end must be integer code-point offsets"
            )
        if self.start < 0 or self.end <= self.start:
            raise AnchorError("TextPositionSelector requires 0 <= start < end")
        if self.end - self.start != len(self.exact):
            raise AnchorError(
                "TextPositionSelector range length must equal the exact quote's "
                "Unicode code-point length"
            )

    @property
    def has_position(self) -> bool:
        """Return whether this selector carries a TextPositionSelector."""
        return self.start is not None

    def to_web_annotation(self) -> list[dict[str, Any]]:
        """Return the selector list in Web Annotation JSON form."""
        value: list[dict[str, Any]] = [
            {
                "type": "TextQuoteSelector",
                "exact": self.exact,
                "prefix": self.prefix,
                "suffix": self.suffix,
            }
        ]
        if self.start is not None and self.end is not None:
            value.append(
                {
                    "type": "TextPositionSelector",
                    "start": self.start,
                    "end": self.end,
                }
            )
        return value

    def to_json(self) -> str:
        """Serialize this selector deterministically for ``selector_json``."""
        return serialize_selector(self)

    @classmethod
    def from_web_annotation(cls, value: object) -> "CompositeSelector":
        """Parse a Web Annotation selector list or a ``selector`` wrapper."""
        if isinstance(value, Mapping):
            if "selector" not in value:
                raise AnchorError(
                    "selector JSON object must contain a selector property"
                )
            value = value["selector"]
        if not isinstance(value, list):
            raise AnchorError("selector JSON must contain a selector list")

        quote: Mapping[str, Any] | None = None
        position: Mapping[str, Any] | None = None
        for item in value:
            if not isinstance(item, Mapping):
                raise AnchorError("each Web Annotation selector must be an object")
            selector_type = item.get("type")
            if selector_type == "TextQuoteSelector":
                if quote is not None:
                    raise AnchorError(
                        "selector JSON contains duplicate quote selectors"
                    )
                quote = item
            elif selector_type == "TextPositionSelector":
                if position is not None:
                    raise AnchorError(
                        "selector JSON contains duplicate position selectors"
                    )
                position = item
            else:
                raise AnchorError(
                    f"unsupported Web Annotation selector type: {selector_type!r}"
                )

        if quote is None:
            raise AnchorError("selector JSON requires one TextQuoteSelector")
        return cls(
            exact=quote.get("exact"),
            prefix=quote.get("prefix", ""),
            suffix=quote.get("suffix", ""),
            start=position.get("start") if position is not None else None,
            end=position.get("end") if position is not None else None,
        )

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> "CompositeSelector":
        """Parse a serialized selector."""
        return parse_selector(raw)


def serialize_selector(selector: CompositeSelector) -> str:
    """Serialize a composite selector with stable key ordering."""
    if not isinstance(selector, CompositeSelector):
        raise TypeError("selector must be a CompositeSelector")
    return json.dumps(
        selector.to_web_annotation(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def parse_selector(raw: str | bytes | bytearray) -> CompositeSelector:
    """Parse ``selector_json`` into a validated composite selector."""
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise AnchorError(f"selector JSON is invalid: {exc}") from exc
    return CompositeSelector.from_web_annotation(value)


def verify_snapshot_sha256(text: str, expected_sha256: str) -> str:
    """Require an immutable text snapshot to match its captured SHA-256."""
    if not isinstance(expected_sha256, str):
        raise AnchorError("snapshot SHA-256 must be a hexadecimal string")
    expected = expected_sha256.strip().lower()
    if not _SHA256_RE.fullmatch(expected):
        raise AnchorError("snapshot SHA-256 must be a 64-character hexadecimal digest")
    actual = sha256_text(text)
    if not hmac.compare_digest(expected, actual):
        raise AnchorError(
            f"snapshot SHA-256 mismatch: expected {expected}, got {actual}"
        )
    return actual


def validate_position(
    text: str,
    selector: CompositeSelector,
) -> ResolvedAnchor:
    """Validate the position selector against the exact quote in ``text``."""
    if selector.start is None or selector.end is None:
        raise AnchorError("selector has no TextPositionSelector")
    if selector.end > len(text):
        raise AnchorError(
            "TextPositionSelector is outside the snapshot's Unicode code-point "
            f"length {len(text)}: {selector.start}:{selector.end}"
        )
    actual = text[selector.start : selector.end]
    if actual != selector.exact:
        raise AnchorError(
            "TextPositionSelector quote mismatch at Unicode code-point offsets "
            f"{selector.start}:{selector.end}. Expected {selector.exact!r}, "
            f"found {actual!r}"
        )
    return ResolvedAnchor(selector.start, selector.end, actual)


def locate_all(text: str, exact: str) -> tuple[ResolvedAnchor, ...]:
    """Find every exact quote, or every whitespace-tolerant match if none exist.

    This is the AOV quote firewall's exact-first resolution rule. The fallback
    changes only internal whitespace runs and preserves every non-whitespace
    character.
    """
    if not exact or not exact.strip():
        return ()

    matches: list[ResolvedAnchor] = []
    offset = text.find(exact)
    while offset >= 0:
        matches.append(
            ResolvedAnchor(
                offset, offset + len(exact), text[offset : offset + len(exact)]
            )
        )
        offset = text.find(exact, offset + 1)
    if matches:
        return tuple(matches)

    parts = exact.split()
    if not parts:
        return ()
    pattern = re.compile(r"\s+".join(re.escape(part) for part in parts))
    return tuple(
        ResolvedAnchor(match.start(), match.end(), text[match.start() : match.end()])
        for match in pattern.finditer(text)
    )


def _matches_context(
    text: str,
    candidate: ResolvedAnchor,
    selector: CompositeSelector,
) -> bool:
    prefix_matches = not selector.prefix or text[: candidate.start].endswith(
        selector.prefix
    )
    suffix_matches = not selector.suffix or text[candidate.end :].startswith(
        selector.suffix
    )
    return prefix_matches and suffix_matches


def _valid_position_candidate(
    text: str,
    selector: CompositeSelector,
    candidates: tuple[ResolvedAnchor, ...],
) -> ResolvedAnchor | None:
    if not selector.has_position:
        return None
    try:
        positioned = validate_position(text, selector)
    except AnchorError:
        return None
    return positioned if positioned in candidates else None


def reanchor(
    text: str,
    selector: CompositeSelector,
    *,
    expected_snapshot_sha256: str | None = None,
) -> ResolvedAnchor:
    """Resolve a selector without guessing.

    Exact quote candidates win over whitespace-tolerant candidates. Quote
    context and a still-valid position selector may disambiguate repeated text.
    Immutable evidence should pass ``expected_snapshot_sha256`` so any content
    drift fails before quote resolution.
    """
    strict_position: ResolvedAnchor | None = None
    if expected_snapshot_sha256 is not None:
        verify_snapshot_sha256(text, expected_snapshot_sha256)
        if selector.has_position:
            strict_position = validate_position(text, selector)

    candidates = locate_all(text, selector.exact)
    if not candidates:
        raise AnchorError(f"anchor quote was not found: {selector.exact!r}")
    if len(candidates) == 1:
        candidate = candidates[0]
        if strict_position is not None and strict_position != candidate:
            raise AnchorError(
                "selector conflict: the immutable position and quote resolve to "
                "different spans"
            )
        return candidate

    has_context = bool(selector.prefix or selector.suffix)
    contextual = (
        tuple(
            candidate
            for candidate in candidates
            if _matches_context(text, candidate, selector)
        )
        if has_context
        else candidates
    )
    positioned = strict_position or _valid_position_candidate(
        text, selector, candidates
    )

    if len(contextual) == 1:
        contextual_match = contextual[0]
        if positioned is not None and positioned != contextual_match:
            raise AnchorError(
                "selector conflict: quote context and position resolve to "
                "different spans"
            )
        return contextual_match

    if positioned is not None:
        if contextual and positioned not in contextual:
            raise AnchorError(
                "selector conflict: position does not match any contextual candidate"
            )
        return positioned

    offsets = ", ".join(
        f"{candidate.start}:{candidate.end}" for candidate in candidates
    )
    detail = (
        " Supplied prefix and suffix did not identify exactly one candidate."
        if has_context
        else " Add prefix, suffix, or a valid position selector."
    )
    raise AnchorError(
        f"anchor quote is ambiguous at {len(candidates)} spans: {offsets}.{detail}"
    )
