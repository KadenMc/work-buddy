"""Pure display projections over an already-fetched execution catalog."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def project_selection_labels(
    selection: Mapping[str, Any], providers: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Copy a selection with current catalog labels, without changing authority.

    No catalog discovery or persistence occurs here. Missing/retired entries
    retain their saved labels; IDs, revisions and provenance are untouched.
    """
    result = dict(selection)
    provider = next(
        (item for item in providers if item.get("id") == selection.get("provider_id")),
        None,
    )
    if provider is None:
        return result
    provider_label = provider.get("label")
    if isinstance(provider_label, str) and provider_label.strip():
        result["provider_label"] = provider_label
    models = provider.get("models")
    model = next(
        (
            item
            for item in (models if isinstance(models, Sequence) else ())
            if isinstance(item, Mapping) and item.get("id") == selection.get("model_id")
        ),
        None,
    )
    if model is not None:
        model_label = model.get("label")
        if isinstance(model_label, str) and model_label.strip():
            result["model_label"] = model_label
    return result
