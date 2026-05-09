"""Tests for the verdict schema's Anthropic-strict-mode compatibility.

Anthropic's structured-output mode requires every ``"type": "object"`` (or
nullable-object ``["object", "null"]``) field that declares ``properties`` to
also declare ``additionalProperties: false`` explicitly. A missing one fails
the entire call with HTTP 400 — and Anthropic's check is whole-schema, so the
worst-positioned violation hides any others.

The recursive walk in :func:`_walk` mirrors the audit script that surfaced
the original Slice-4 ``risk_profile`` regression on the first live
inline_capture verdict call (see DECISIONS.md D-010). Locking it in as a
unit test means any future schema addition that forgets
``additionalProperties: false`` fails CI rather than fails 400 in production.
"""

from __future__ import annotations

from typing import Any

from work_buddy.clarify.verdict_schema import MULTI_RECORD_VERDICT_SCHEMA


def _is_object_type(t: Any) -> bool:
    if t == "object":
        return True
    if isinstance(t, list) and "object" in t:
        return True
    return False


def _walk(node: Any, path: str, problems: list[str]) -> None:
    if isinstance(node, list):
        for i, item in enumerate(node):
            _walk(item, f"{path}[{i}]", problems)
        return
    if not isinstance(node, dict):
        return

    if _is_object_type(node.get("type")) and "properties" in node:
        if node.get("additionalProperties") is not False:
            problems.append(
                f"{path}: type={node.get('type')!r} has 'properties' but "
                f"additionalProperties is "
                f"{node.get('additionalProperties')!r} (must be False)"
            )

    for k, v in node.items():
        _walk(v, f"{path}.{k}", problems)


def test_multi_record_verdict_schema_is_anthropic_strict_compliant() -> None:
    """Every nested object-type field must declare additionalProperties:false.

    Regression guard for the Slice-4 risk_profile bug surfaced at first
    live verdict call (see DECISIONS.md D-010).
    """
    problems: list[str] = []
    _walk(MULTI_RECORD_VERDICT_SCHEMA, "$", problems)
    assert not problems, (
        "MULTI_RECORD_VERDICT_SCHEMA has objects missing "
        "additionalProperties:false (Anthropic strict mode rejects):\n  "
        + "\n  ".join(problems)
    )


def _walk_enum_compat(node: Any, path: str, problems: list[str]) -> None:
    """Walk for enum/type-union mismatches.

    Anthropic strict structured-output rejects schemas that mix Python
    ``None`` (i.e. JSON null) into ``enum`` arrays. The acceptable
    pattern is to constrain string values via ``enum`` and express
    null acceptance separately via the type union.
    """
    if isinstance(node, list):
        for i, item in enumerate(node):
            _walk_enum_compat(item, f"{path}[{i}]", problems)
        return
    if not isinstance(node, dict):
        return

    if "enum" in node and isinstance(node["enum"], list):
        if any(v is None for v in node["enum"]):
            problems.append(
                f"{path}: enum contains None — Anthropic strict mode rejects. "
                f"Drop None and rely on ``type: ['string', 'null']`` for null."
            )

    for k, v in node.items():
        _walk_enum_compat(v, f"{path}.{k}", problems)


def test_multi_record_verdict_schema_no_null_in_enums() -> None:
    """Enum fields must not contain Python None (JSON null).

    Anthropic's strict mode rejects ``"enum": ["foo", "bar", None]`` even
    when the field's type is ``["string", "null"]``. Express nullability
    via the type union; keep enum entries homogeneously string.

    Regression guard for the Slice-5a required_contexts_source bug
    surfaced at second live verdict call (see DECISIONS.md D-011).
    """
    problems: list[str] = []
    _walk_enum_compat(MULTI_RECORD_VERDICT_SCHEMA, "$", problems)
    assert not problems, (
        "MULTI_RECORD_VERDICT_SCHEMA has enum/null violations:\n  "
        + "\n  ".join(problems)
    )
