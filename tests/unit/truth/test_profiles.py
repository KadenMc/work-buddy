from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from work_buddy.truth.contracts import ProfileError
from work_buddy.truth.profiles import (
    dump_profile,
    load_profile,
    normalize_store_id,
    validate_new_claim,
    validate_profile,
)


FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "truth"
KNOWN_FIXTURE_OPERATIONS = frozenset(
    {
        "capture",
        "mark_span",
        "propose",
        "confirm",
        "supersede",
        "derive",
        "sweep",
        "materialize",
        "expire",
        "redact",
    }
)


def _valid_profile() -> dict[str, Any]:
    return {
        "store_id": "12345678-1234-4234-8234-123456789ABC",
        "profile": "project-canon",
        "name": "Project canon",
        "allowed_claim_kinds": ["fact", "custom_metric"],
        "required_fields": {
            "fact": ["subject.id", "predicate", "value.text"],
            "custom_metric": [],
        },
        "gate": {
            "rejected_content": "redact",
            "confirmation_surfaces": ["dashboard", "custom_fold"],
            "block_materialize_on_flags": True,
            "review_queue": {"priority": "normal"},
        },
        "projection": "resident",
        "export_committed": True,
        "proposal_max_age": "30d",
        "validators": {
            "citation_required": {
                "claim_kinds": ["fact"],
                "extension_mode": "strict",
            }
        },
        "extensions": {"vendor.example/layout": "compact"},
        "future_profile_key": {"enabled": True},
    }


def test_validate_profile_normalizes_identity_and_accepts_name_alias() -> None:
    profile = validate_profile(_valid_profile())

    assert profile.store_id == "12345678123442348234123456789abc"
    assert profile.profile == "project-canon"
    assert profile.title == "Project canon"
    assert profile.allowed_claim_kinds == ("fact", "custom_metric")
    assert profile.required_fields["fact"] == (
        "subject.id",
        "predicate",
        "value.text",
    )
    assert profile.gate.confirmation_surfaces == ("dashboard", "custom_fold")
    assert profile.proposal_max_age_seconds == 30 * 24 * 60 * 60


def test_dump_and_load_preserve_open_extension_metadata(tmp_path: Path) -> None:
    original = _valid_profile()
    expected_validators = copy.deepcopy(original["validators"])
    expected_extensions = copy.deepcopy(original["extensions"])
    expected_gate_extension = copy.deepcopy(original["gate"]["review_queue"])
    expected_extra = copy.deepcopy(original["future_profile_key"])

    written = dump_profile(original, tmp_path)
    loaded = load_profile(tmp_path / ".wb-truth")

    assert written == tmp_path / ".wb-truth" / "store.yaml"
    assert loaded.validators == expected_validators
    assert loaded.extensions == expected_extensions
    assert loaded.gate.extensions["review_queue"] == expected_gate_extension
    assert loaded.extra["future_profile_key"] == expected_extra
    rendered = yaml.safe_load(written.read_text(encoding="utf-8"))
    assert rendered["future_profile_key"] == expected_extra
    assert rendered["gate"]["review_queue"] == expected_gate_extension


def _set_store_id(profile: dict[str, Any]) -> None:
    profile["store_id"] = "not-a-uuid"


def _set_profile_name(profile: dict[str, Any]) -> None:
    profile["profile"] = "Project Canon"


def _remove_title(profile: dict[str, Any]) -> None:
    profile.pop("name")


def _empty_claim_kinds(profile: dict[str, Any]) -> None:
    profile["allowed_claim_kinds"] = []


def _duplicate_claim_kinds(profile: dict[str, Any]) -> None:
    profile["allowed_claim_kinds"] = ["fact", "fact"]


def _unknown_required_kind(profile: dict[str, Any]) -> None:
    profile["required_fields"]["decision"] = ["value"]


def _invalid_dotted_path(profile: dict[str, Any]) -> None:
    profile["required_fields"]["fact"] = ["subject..id"]


def _invalid_rejection_policy(profile: dict[str, Any]) -> None:
    profile["gate"]["rejected_content"] = "archive"


def _empty_confirmation_surfaces(profile: dict[str, Any]) -> None:
    profile["gate"]["confirmation_surfaces"] = []


def _invalid_materialize_flag(profile: dict[str, Any]) -> None:
    profile["gate"]["block_materialize_on_flags"] = "yes"


def _invalid_projection(profile: dict[str, Any]) -> None:
    profile["projection"] = "sometimes"


def _invalid_export_flag(profile: dict[str, Any]) -> None:
    profile["export_committed"] = 1


def _invalid_proposal_age(profile: dict[str, Any]) -> None:
    profile["proposal_max_age"] = "0d"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_set_store_id, "store_id"),
        (_set_profile_name, "profile must"),
        (_remove_title, "title or name"),
        (_empty_claim_kinds, "at least one"),
        (_duplicate_claim_kinds, "duplicates"),
        (_unknown_required_kind, "disallowed claim kind"),
        (_invalid_dotted_path, "invalid dotted paths"),
        (_invalid_rejection_policy, "redact or retain"),
        (_empty_confirmation_surfaces, "at least one"),
        (_invalid_materialize_flag, "true or false"),
        (_invalid_projection, "projection must"),
        (_invalid_export_flag, "true or false"),
        (_invalid_proposal_age, "proposal_max_age"),
    ],
)
def test_profile_validation_rejects_invalid_contracts(
    mutate: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    raw = _valid_profile()
    mutate(raw)

    with pytest.raises(ProfileError, match=message):
        validate_profile(raw)


def test_title_and_name_must_match_when_both_are_declared() -> None:
    raw = _valid_profile()
    raw["title"] = "A different title"

    with pytest.raises(ProfileError, match="must match"):
        validate_profile(raw)


def test_normalize_store_id_accepts_plain_hex_and_canonical_uuid() -> None:
    expected = "12345678123442348234123456789abc"
    assert normalize_store_id(expected.upper()) == expected
    assert normalize_store_id("12345678-1234-4234-8234-123456789ABC") == expected


def test_validate_new_claim_checks_nested_required_fields_and_surface() -> None:
    profile = validate_profile(_valid_profile())

    validate_new_claim(
        profile,
        claim_kind="fact",
        structured={
            "subject": {"id": "project:alpha"},
            "predicate": "has_state",
            "value": {"text": "ready"},
        },
        confirmation_surface="custom_fold",
    )
    validate_new_claim(
        profile,
        claim_kind="fact",
        structured=(
            '{"subject":{"id":"project:alpha"},'
            '"predicate":"has_state","value":{"text":"ready"}}'
        ),
    )


def test_validate_new_claim_rejects_missing_fields_kind_and_surface() -> None:
    profile = validate_profile(_valid_profile())

    with pytest.raises(ProfileError, match="value.text"):
        validate_new_claim(
            profile,
            claim_kind="fact",
            structured={
                "subject": {"id": "project:alpha"},
                "predicate": "has_state",
                "value": {"text": " "},
            },
        )
    with pytest.raises(ProfileError, match="not allowed"):
        validate_new_claim(profile, claim_kind="retired_fact")
    with pytest.raises(ProfileError, match="confirmation surface"):
        validate_new_claim(
            profile,
            claim_kind="custom_metric",
            confirmation_surface="unregistered_plugin",
        )


def test_tightened_profile_does_not_revalidate_existing_history() -> None:
    original = _valid_profile()
    original["allowed_claim_kinds"].append("retired_fact")
    original["required_fields"]["retired_fact"] = ["value"]
    old_policy = validate_profile(original)
    validate_new_claim(
        old_policy,
        claim_kind="retired_fact",
        structured={"value": "already stored"},
    )

    tightened = validate_profile(_valid_profile())
    assert "retired_fact" not in tightened.allowed_claim_kinds

    with pytest.raises(ProfileError, match="not allowed"):
        validate_new_claim(
            tightened,
        claim_kind="retired_fact",
            structured={"value": "a new write"},
        )


def _fixture_paths() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.yaml"))


def _walk_references(value: Any) -> list[str]:
    references: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_ref") and isinstance(item, str):
                references.append(item)
            references.extend(_walk_references(item))
    elif isinstance(value, list):
        for item in value:
            references.extend(_walk_references(item))
    return references


def test_declarative_workload_fixture_set_is_complete_and_executable() -> None:
    paths = _fixture_paths()
    assert [path.name for path in paths] == [
        "cothink_micro_confirmation.yaml",
        "electricrag_supersession.yaml",
        "my_career_artifact.yaml",
    ]

    for path in paths:
        fixture = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert fixture["fixture_version"] == "wb-truth-fixture/v1"
        assert fixture["name"]
        profile = validate_profile(fixture["profile"])

        aliases = fixture["ids"]
        assert aliases
        normalized_ids = [normalize_store_id(value) for value in aliases.values()]
        assert len(normalized_ids) == len(set(normalized_ids))

        steps = fixture["steps"]
        assert steps
        for step in steps:
            assert step["op"] in KNOWN_FIXTURE_OPERATIONS
            assert isinstance(step["expect"], dict) and step["expect"]
            if "id" in step:
                assert step["id"] in aliases
            for reference in _walk_references(step):
                assert reference in aliases
            if step["op"] == "propose":
                validate_new_claim(
                    profile,
                    claim_kind=step["input"]["claim_kind"],
                    structured=step["input"].get("structured"),
                )
            if step["op"] == "confirm":
                validate_new_claim(
                    profile,
                    claim_kind=step["input"]["claim_kind"],
                    structured=step["input"].get("structured"),
                    confirmation_surface=step["input"]["surface"],
                )

        assert isinstance(fixture["expected_outcomes"], dict)
        assert fixture["expected_outcomes"]
