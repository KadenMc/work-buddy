from __future__ import annotations

import pytest

from work_buddy.truth.fingerprints import (
    FingerprintStatus,
    IMMUTABLE_LINK_TYPES,
    compute_target_fingerprint,
    fingerprint_status,
    is_fingerprint_current,
    is_fingerprint_reviewed,
    is_fingerprint_stale,
)


@pytest.mark.parametrize("link_type", ["about_entity", "cites_external"])
def test_mutable_link_fingerprints_change_with_target_content(link_type: str) -> None:
    original = compute_target_fingerprint(link_type, "target content")
    changed = compute_target_fingerprint(link_type, "changed target content")

    assert original is not None
    assert len(original) == 64
    assert original != changed


def test_structured_target_fingerprint_is_canonical() -> None:
    first = compute_target_fingerprint(
        "about_entity",
        {"description": "A  person", "aliases": ["K", "Kaden"]},
    )
    second = compute_target_fingerprint(
        "about_entity",
        {"aliases": ["K", "Kaden"], "description": "A person"},
    )

    assert first == second


@pytest.mark.parametrize("link_type", sorted(IMMUTABLE_LINK_TYPES))
def test_immutable_link_types_never_get_fingerprints(link_type: str) -> None:
    assert compute_target_fingerprint(link_type, "content") is None
    assert compute_target_fingerprint(link_type) is None


def test_future_document_link_types_are_not_enabled() -> None:
    with pytest.raises(ValueError, match="unsupported link_type"):
        compute_target_fingerprint("expresses_document", "content")


def test_fingerprint_helpers_distinguish_current_stale_and_unreviewed() -> None:
    reviewed = compute_target_fingerprint("about_entity", "reviewed content")
    changed = compute_target_fingerprint("about_entity", "changed content")
    assert reviewed is not None
    assert changed is not None

    assert (
        fingerprint_status("about_entity", reviewed, reviewed)
        is FingerprintStatus.CURRENT
    )
    assert is_fingerprint_reviewed("about_entity", reviewed, reviewed)
    assert is_fingerprint_current("about_entity", reviewed, reviewed)
    assert not is_fingerprint_stale("about_entity", reviewed, reviewed)

    assert (
        fingerprint_status("about_entity", reviewed, changed)
        is FingerprintStatus.STALE
    )
    assert is_fingerprint_stale("about_entity", reviewed, changed)
    assert is_fingerprint_stale("about_entity", reviewed, None)
    assert not is_fingerprint_reviewed("about_entity", reviewed, changed)

    assert (
        fingerprint_status("about_entity", None, changed)
        is FingerprintStatus.UNREVIEWED
    )
    assert not is_fingerprint_stale("about_entity", None, changed)


def test_immutable_fingerprint_status_is_not_applicable() -> None:
    assert (
        fingerprint_status("supersedes", None, None)
        is FingerprintStatus.NOT_APPLICABLE
    )
    assert not is_fingerprint_current("supersedes", None, None)
    assert not is_fingerprint_stale("supersedes", None, None)
