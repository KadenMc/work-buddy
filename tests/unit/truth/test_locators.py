from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

import pytest

from work_buddy.truth.locators import (
    LocatorError,
    LocatorRegistry,
    SchemeValidation,
)


SHA256 = "ab" * 32
CNT_SHA1 = "a1" * 20
REV_SHA1 = "b2" * 20
SNP_SHA1 = "c3" * 20


def _qualified_swhid(**qualifiers: str) -> str:
    values = {
        "origin": "https://GitHub.com/Example/Répo.git",
        "anchor": f"swh:1:rev:{REV_SHA1}",
        "path": "/src/café.py",
        "lines": "1",
        **qualifiers,
    }
    rendered = ";".join(f"{key}={value}" for key, value in values.items())
    return f"SWH:1:CNT:{CNT_SHA1.upper()};{rendered}"


def test_registry_exposes_all_first_party_schemes() -> None:
    registry = LocatorRegistry()

    assert registry.schemes == (
        "arxiv",
        "doi",
        "file",
        "http",
        "https",
        "pmid",
        "swh",
        "wb-session",
    )


def test_qualified_swhid_normalizes_core_qualifier_order_and_pinpoint() -> None:
    locator = (
        f"SWH:1:CNT:{CNT_SHA1.upper()}"
        ";lines=009-015"
        ";path=/src/café.py"
        f";visit=SWH:1:SNP:{SNP_SHA1.upper()}"
        f";anchor=SWH:1:REV:{REV_SHA1.upper()}"
        ";origin=HTTPS://GitHub.com/Example/Répo.git"
    )

    result = LocatorRegistry().validate(
        "document",
        locator,
        {"permalink_template": "https://github.com/example/repo/blob/{commit}/{path}"},
        SHA256.upper(),
    )

    assert result.locator == (
        f"swh:1:cnt:{CNT_SHA1}"
        ";origin=https://github.com/Example/R%C3%A9po.git"
        f";visit=swh:1:snp:{SNP_SHA1}"
        f";anchor=swh:1:rev:{REV_SHA1}"
        ";path=/src/caf%C3%A9.py"
        ";lines=9-15"
    )
    assert result.locator_scheme == "swh"
    assert result.verifiability_class == "A"
    assert result.integrity_recipe == {
        "algorithm": "git-blob-sha1",
        "expected_digest": CNT_SHA1,
        "line_range": [9, 15],
        "method": "recompute_swhid_content_hash",
        "network_required": False,
        "qualifiers": {
            "anchor": f"swh:1:rev:{REV_SHA1}",
            "lines": "9-15",
            "origin": "https://github.com/Example/R%C3%A9po.git",
            "path": "/src/caf%C3%A9.py",
            "visit": f"swh:1:snp:{SNP_SHA1}",
        },
        "snapshot_sha256": SHA256,
    }
    assert result.meta["permalink_template"].endswith("/{path}")
    assert result.meta["locator_scheme"] == "swh"


@pytest.mark.parametrize(
    ("locator", "message"),
    [
        (
            f"swh:1:rev:{REV_SHA1};origin=https://example.test;"
            f"anchor=swh:1:rev:{REV_SHA1};path=/a.py;lines=1",
            "requires an swh:1:cnt",
        ),
        (f"swh:1:cnt:{CNT_SHA1}", "requires origin"),
        (_qualified_swhid(extra="value"), "unsupported SWHID qualifier"),
        (_qualified_swhid(path="/src/../secret.py"), "path traversal"),
        (_qualified_swhid(lines="15-9"), "cannot end before"),
        (_qualified_swhid(anchor=f"swh:1:cnt:{CNT_SHA1}"), "anchor must"),
    ],
)
def test_swhid_validation_fails_closed(locator: str, message: str) -> None:
    with pytest.raises(LocatorError, match=message):
        LocatorRegistry().validate("document", locator, {}, SHA256)


def test_swhid_requires_the_human_display_permalink() -> None:
    with pytest.raises(LocatorError, match="permalink_template"):
        LocatorRegistry().validate("document", _qualified_swhid(), {}, SHA256)


def test_bare_web_url_requires_retrieval_state_and_is_class_d() -> None:
    result = LocatorRegistry().validate(
        "web",
        "HTTP://Example.COM",
        {"retrieved_at": "2026-07-11T12:30:00-04:00"},
    )

    assert result.locator == "http://example.com/"
    assert result.verifiability_class == "D"
    assert result.meta == {
        "locator_scheme": "http",
        "retrieved_at": "2026-07-11T16:30:00.000Z",
    }
    assert result.integrity_recipe["method"] == "check_live_url_and_capture_drift"
    assert result.integrity_recipe["network_required"] is True

    with pytest.raises(LocatorError, match="requires retrieved_at"):
        LocatorRegistry().validate("web", "https://example.com/page", {})


def test_web_archive_or_matching_local_snapshot_promotes_to_class_b() -> None:
    registry = LocatorRegistry()
    archived = registry.validate(
        "web",
        "https://example.org/page",
        {
            "retrieved_at": "2026-07-11T16:00:00Z",
            "archived_uri": (
                "https://web.archive.org/web/20260711160000/"
                "https://example.org/page"
            ),
        },
    )
    local = registry.validate(
        "web",
        "https://example.org/page",
        {
            "retrieved_datetime": "2026-07-11T16:00:00+00:00",
            "snapshot_sha256": SHA256.upper(),
        },
        SHA256,
    )

    assert archived.verifiability_class == "B"
    assert archived.integrity_recipe["archived_uri"].startswith(
        "https://web.archive.org/"
    )
    assert archived.integrity_recipe["network_required"] is True
    assert local.verifiability_class == "B"
    assert local.meta["snapshot_sha256"] == SHA256
    assert local.integrity_recipe["network_required"] is False


def test_web_snapshot_hash_and_retrieval_aliases_must_match() -> None:
    registry = LocatorRegistry()

    with pytest.raises(LocatorError, match="does not match content_sha256"):
        registry.validate(
            "web",
            "https://example.org/page",
            {
                "retrieved_at": "2026-07-11T16:00:00Z",
                "snapshot_sha256": "cd" * 32,
            },
            SHA256,
        )
    with pytest.raises(LocatorError, match="do not match"):
        registry.validate(
            "web",
            "https://example.org/page",
            {
                "retrieved_at": "2026-07-11T16:00:00Z",
                "retrieved_datetime": "2026-07-12T16:00:00Z",
            },
        )
    with pytest.raises(LocatorError, match="local archived_uri"):
        registry.validate(
            "web",
            "https://example.org/page",
            {
                "retrieved_at": "2026-07-11T16:00:00Z",
                "archived_uri": "file:///C:/captures/page.html",
            },
        )


def test_unicode_web_uri_is_normalized_without_fetching() -> None:
    result = LocatorRegistry().validate(
        "web",
        "https://例え.テスト/資料?q=✓#節",
        {"retrieved_at": "2026-07-11T16:00:00Z"},
    )

    assert result.locator == (
        "https://xn--r8jz45g.xn--zckzah/"
        "%E8%B3%87%E6%96%99?q=%E2%9C%93#%E7%AF%80"
    )


@pytest.mark.parametrize(
    ("locator", "normalized", "resolver"),
    [
        (
            "DOI:10.1234/ABC.DEF",
            "doi:10.1234/abc.def",
            "https://doi.org/10.1234/abc.def",
        ),
        (
            "ARXIV:1512.00567V2",
            "arxiv:1512.00567v2",
            "https://arxiv.org/abs/1512.00567v2",
        ),
        (
            "PMID:26360422",
            "pmid:26360422",
            "https://pubmed.ncbi.nlm.nih.gov/26360422/",
        ),
    ],
)
def test_academic_identifiers_are_class_c_without_snapshot(
    locator: str,
    normalized: str,
    resolver: str,
) -> None:
    result = LocatorRegistry().validate(
        "document",
        locator,
        {"csl_json": {"title": "A source"}},
    )

    assert result.locator == normalized
    assert result.verifiability_class == "C"
    assert result.integrity_recipe["resolver_uri"] == resolver
    assert result.integrity_recipe["match_csl_json"] is True
    assert result.integrity_recipe["network_required"] is True


def test_academic_snapshot_promotes_to_b_and_normalizes_pinpoint() -> None:
    result = LocatorRegistry().validate(
        "document",
        "doi:10.5555/EXAMPLE",
        {
            "csl_json": {"title": "Example"},
            "pinpoint": {
                "locator": " 42-44 ",
                "label": "Page",
                "extension": {"z": 2, "a": 1},
            }
        },
        SHA256.upper(),
    )

    assert result.verifiability_class == "B"
    assert result.meta["pinpoint"] == {
        "extension": {"a": 1, "z": 2},
        "label": "page",
        "locator": "42-44",
    }
    assert result.meta["snapshot_sha256"] == SHA256
    assert result.integrity_recipe["method"] == "verify_academic_snapshot"


@pytest.mark.parametrize(
    "locator",
    [
        "doi:not-a-doi",
        "arxiv:2026.12",
        "pmid:00012",
        "doi:10.1234/example#page=2",
    ],
)
def test_malformed_academic_identifiers_are_rejected(locator: str) -> None:
    with pytest.raises(LocatorError):
        LocatorRegistry().validate("document", locator, {})


def test_academic_locator_requires_csl_metadata() -> None:
    with pytest.raises(LocatorError, match="requires csl_json"):
        LocatorRegistry().validate("document", "doi:10.5555/example", {})


@pytest.mark.parametrize("kind", ["chat", "utterance"])
def test_session_locator_requires_and_binds_transcript_digest(kind: str) -> None:
    result = LocatorRegistry().validate(
        kind,
        "WB-SESSION://session-123/msg-π",
        {"surface": "codex"},
        SHA256.upper(),
    )

    assert result.locator == "wb-session://session-123/msg-%CF%80"
    assert result.verifiability_class == "B"
    assert result.meta["transcript_sha256"] == SHA256
    assert result.integrity_recipe == {
        "expected_sha256": SHA256,
        "message_ref": "msg-π",
        "method": "verify_transcript_snapshot",
        "network_required": False,
        "session_id": "session-123",
    }


@pytest.mark.parametrize(
    ("locator", "meta", "digest", "message"),
    [
        ("wb-session://session-123/msg", {}, None, "content_sha256 is required"),
        ("wb-session://session-123/a/b", {}, SHA256, "exactly one"),
        ("wb-session://session-123/%2e%2e", {}, SHA256, "not path safe"),
        (
            "wb-session://session-123/msg",
            {"transcript_sha256": "cd" * 32},
            SHA256,
            "does not match",
        ),
    ],
)
def test_session_locator_rejects_missing_mismatched_or_unsafe_data(
    locator: str,
    meta: Mapping[str, Any],
    digest: str | None,
    message: str,
) -> None:
    with pytest.raises(LocatorError, match=message):
        LocatorRegistry().validate("chat", locator, meta, digest)


@pytest.mark.parametrize(
    ("locator", "normalized"),
    [
        (
            r"c:\Vault\Café\Note.md",
            "file:///C:/Vault/Caf%C3%A9/Note.md",
        ),
        (
            "file:///home/kaden/notes/naïve.md",
            "file:///home/kaden/notes/na%C3%AFve.md",
        ),
        (
            r"\\server\share\Truth\Note.md",
            "file://server/share/Truth/Note.md",
        ),
    ],
)
def test_local_file_normalizes_absolute_paths_and_requires_snapshot(
    locator: str,
    normalized: str,
) -> None:
    result = LocatorRegistry().validate("artifact", locator, {}, SHA256.upper())

    assert result.locator == normalized
    assert result.locator_scheme == "file"
    assert result.verifiability_class == "A"
    assert result.meta["snapshot_sha256"] == SHA256
    assert result.integrity_recipe["requires_snapshot_bytes"] is True
    assert result.integrity_recipe["expected_sha256"] == SHA256


@pytest.mark.parametrize(
    ("locator", "message"),
    [
        ("notes/file.md", "registered URI scheme"),
        ("file:notes/file.md", "absolute"),
        ("file:///C:/Vault/../secret.md", "path traversal"),
        ("file:///C:/Vault/%2E%2E/secret.md", "path traversal"),
        ("file://../secret.md", "invalid host"),
        ("file:////server/share/secret.md", "server in its authority"),
    ],
)
def test_file_locator_rejects_relative_and_traversal_paths(
    locator: str,
    message: str,
) -> None:
    with pytest.raises(LocatorError, match=message):
        LocatorRegistry().validate("document", locator, {}, SHA256)

    if locator.startswith("file"):
        with pytest.raises(LocatorError):
            LocatorRegistry().validate("document", locator, {})


def test_kind_compatibility_is_explicit_and_import_remains_broad() -> None:
    registry = LocatorRegistry()

    with pytest.raises(LocatorError, match="incompatible"):
        registry.validate(
            "document",
            "https://example.org/page",
            {"retrieved_at": "2026-07-11T16:00:00Z"},
        )

    imported = registry.validate(
        "import",
        "https://example.org/page",
        {"retrieved_at": "2026-07-11T16:00:00Z"},
    )
    assert imported.kind == "import"
    assert imported.locator_scheme == "https"


def test_locator_scheme_mismatch_unknown_scheme_and_malformed_uri_fail_closed() -> None:
    registry = LocatorRegistry()

    with pytest.raises(LocatorError, match="does not match"):
        registry.validate(
            "web",
            "https://example.org/page",
            {
                "locator_scheme": "http",
                "retrieved_at": "2026-07-11T16:00:00Z",
            },
        )
    with pytest.raises(LocatorError, match="unregistered"):
        registry.validate("import", "ftp://example.org/file", {})
    with pytest.raises(LocatorError, match="credentials"):
        registry.validate(
            "web",
            "https://user:secret@example.org/page",
            {"retrieved_at": "2026-07-11T16:00:00Z"},
        )
    with pytest.raises(LocatorError, match="evidence kind"):
        registry.validate("legacy", "doi:10.1234/example", {})


def test_metadata_is_preserved_sorted_deterministic_and_input_is_unchanged() -> None:
    meta = {
        "z_extension": [3, {"z": 2, "a": 1}],
        "locator_scheme": "HTTPS",
        "retrieved_datetime": "2026-07-11T16:00:00+00:00",
        "a_extension": {"nested": "value"},
    }
    original = copy.deepcopy(meta)
    registry = LocatorRegistry()

    first = registry.validate("web", "HTTPS://EXAMPLE.ORG:443/é", meta)
    second = registry.validate("web", "HTTPS://EXAMPLE.ORG:443/é", meta)

    assert meta == original
    assert first == second
    assert first.locator == "https://example.org/%C3%A9"
    assert list(first.meta) == sorted(first.meta)
    assert list(first.meta["z_extension"][1]) == ["a", "z"]
    assert json.dumps(first.to_dict(), ensure_ascii=False) == json.dumps(
        second.to_dict(), ensure_ascii=False
    )

    with pytest.raises(LocatorError, match="must be a mapping"):
        registry.validate(
            "web",
            "https://example.org",
            [],  # type: ignore[arg-type]
            None,
        )
    with pytest.raises(LocatorError, match="JSON-compatible"):
        registry.validate(
            "web",
            "https://example.org",
            {
                "retrieved_at": "2026-07-11T16:00:00Z",
                "extension": {"not-json"},
            },
        )


def test_extension_registration_uses_same_fail_closed_contract() -> None:
    def validate_memo(
        kind: str,
        locator: str,
        meta: Mapping[str, Any],
        content_sha256: str | None,
    ) -> SchemeValidation:
        del kind, meta
        if content_sha256 is None:
            raise LocatorError("memo snapshot digest is required")
        prefix, separator, value = locator.partition(":")
        if not separator or prefix.lower() != "memo" or not value.isdigit():
            raise LocatorError("memo locator must contain a decimal id")
        return SchemeValidation(
            locator=f"memo:{int(value)}",
            verifiability_class="B",
            integrity_recipe={
                "expected_sha256": content_sha256,
                "method": "verify_memo_snapshot",
                "network_required": False,
            },
            meta_updates={"memo_version": 1},
        )

    registry = LocatorRegistry(include_builtins=False)
    registry.register("memo", validate_memo, kinds={"artifact"})
    result = registry.validate(
        "artifact",
        "MEMO:00042",
        {"extension": {"enabled": True}},
        SHA256,
    )

    assert result.locator == "memo:42"
    assert result.meta == {
        "extension": {"enabled": True},
        "locator_scheme": "memo",
        "memo_version": 1,
    }
    assert registry.validate("import", "memo:7", {}, SHA256).kind == "import"
    with pytest.raises(LocatorError, match="incompatible"):
        registry.validate("document", "memo:7", {}, SHA256)
    with pytest.raises(LocatorError, match="registered"):
        registry.register("memo", validate_memo, kinds={"artifact"})


def test_extension_recipe_and_output_scheme_are_validated() -> None:
    registry = LocatorRegistry(include_builtins=False)
    registry.register(
        "broken",
        lambda kind, locator, meta, digest: SchemeValidation(
            "other:value",
            "Z",
            {"method": "", "network_required": "no"},
        ),
        kinds={"artifact"},
    )

    with pytest.raises(LocatorError, match="changed the registered"):
        registry.validate("artifact", "broken:value", {})


def test_validation_never_calls_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr("urllib.request.urlopen", fail_network)
    result = LocatorRegistry().validate(
        "document",
        "doi:10.1234/example",
        {"csl_json": {"title": "Stored metadata"}},
    )

    assert result.verifiability_class == "C"
    assert result.integrity_recipe["network_required"] is True
