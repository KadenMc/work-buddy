"""Catalog labels are display-only and never rewrite execution authority."""

from __future__ import annotations

import copy

import pytest

from work_buddy.agent_execution.presentation import project_selection_labels


@pytest.mark.parametrize(
    ("providers", "provider_label", "model_label"),
    [
        (
            [
                {
                    "id": "codex",
                    "label": "Codex",
                    "models": [
                        {"id": "fixture-codex", "label": "Fixture Codex"},
                    ],
                }
            ],
            "Codex",
            "Fixture Codex",
        ),
        ([], "Saved provider", "Saved model"),
        (
            [{"id": "codex", "label": "Codex", "models": []}],
            "Codex",
            "Saved model",
        ),
        (
            [
                {
                    "id": "codex",
                    "label": "",
                    "models": [
                        {"id": "fixture-codex", "label": "", "available": False},
                    ],
                }
            ],
            "Saved provider",
            "Saved model",
        ),
    ],
)
def test_label_projection_is_pure_and_preserves_missing_catalog_fallbacks(
    providers,
    provider_label,
    model_label,
):
    selection = {
        "provider_id": "codex",
        "model_id": "fixture-codex",
        "provider_label": "Saved provider",
        "model_label": "Saved model",
        "revision": "revision-1",
        "persisted": True,
        "provenance": {"untouched": True},
    }
    original_selection = copy.deepcopy(selection)
    original_providers = copy.deepcopy(providers)
    projected = project_selection_labels(selection, providers)
    assert projected == {
        **selection,
        "provider_label": provider_label,
        "model_label": model_label,
    }
    assert selection == original_selection
    assert providers == original_providers
    assert projected is not selection
