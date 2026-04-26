"""Unit tests for the post-write-verify recovery helper.

Covers:
- verify_post_write returns "verified" when the substring/sha256 hint
  matches the file content
- verify_post_write returns "absent" when the file exists but doesn't
  contain the hint
- verify_post_write returns "absent" when the file doesn't exist
- verify_post_write returns "indeterminate" when filesystem read fails
  or vault_root config is missing
- _verify_replace handles malformed sha256 hints gracefully
- _verify_substring is exact substring match (no normalization)

Real-world fixture: data/agents/operations/op_34ab708a.json — the op
that surfaced this entire investigation. Its params include the
addendum content that was successfully written to the Slice 1 task
note despite being reported as failed. The integration test confirms
verify_post_write would have returned "verified" for that op.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from work_buddy.obsidian.errors import ObsidianPostWriteUncertain
from work_buddy.obsidian.post_write_verify import (
    _verify_replace,
    _verify_substring,
    verify_post_write,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path):
    """A throwaway vault root that verify_post_write resolves to."""
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    with patch(
        "work_buddy.obsidian.post_write_verify.load_config",
        return_value={"vault_root": str(vault_root)},
    ):
        yield vault_root


# ---------------------------------------------------------------------------
# _verify_replace
# ---------------------------------------------------------------------------


class TestVerifyReplace:
    def test_matching_sha256_returns_true(self):
        import hashlib
        content = "hello world"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert _verify_replace(content, f"sha256:{digest}") is True

    def test_mismatching_sha256_returns_false(self):
        # File content differs from what the hint encodes.
        assert _verify_replace("actual content", "sha256:" + "0" * 64) is False

    def test_missing_sha256_prefix_returns_false(self):
        """Defensive: a hint without the sha256: prefix is malformed —
        treat as absent rather than crashing."""
        assert _verify_replace("anything", "deadbeef" * 8) is False

    def test_empty_hint_returns_false(self):
        assert _verify_replace("anything", "") is False


# ---------------------------------------------------------------------------
# _verify_substring
# ---------------------------------------------------------------------------


class TestVerifySubstring:
    def test_substring_present_returns_true(self):
        content = "header\n\n## Addendum\n\nbody body body"
        assert _verify_substring(content, "## Addendum") is True

    def test_substring_absent_returns_false(self):
        content = "no addendum here"
        assert _verify_substring(content, "## Addendum") is False

    def test_exact_match_no_normalization(self):
        """Substring search is exact — case differences DON'T match."""
        content = "## addendum"
        assert _verify_substring(content, "## Addendum") is False

    def test_empty_content_with_nonempty_hint(self):
        assert _verify_substring("", "anything") is False

    def test_long_witness(self):
        """The bridge's _make_content_hint uses up to 256 chars; verify
        that long witnesses match correctly."""
        body = "x" * 256
        content = f"prelude\n{body}\nepilog"
        assert _verify_substring(content, body) is True


# ---------------------------------------------------------------------------
# verify_post_write — full helper integration
# ---------------------------------------------------------------------------


class TestVerifyPostWriteVerified:
    """File exists and content matches → 'verified'."""

    def test_replace_mode_verified(self, vault):
        import hashlib
        path = vault / "notes" / "x.md"
        path.parent.mkdir(parents=True)
        content = "the actual file content\n"
        path.write_text(content, encoding="utf-8")

        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        exc = ObsidianPostWriteUncertain(
            "notes/x.md",
            content_hint=f"sha256:{digest}",
            write_mode="replace",
        )
        assert verify_post_write(exc) == "verified"

    def test_insert_mode_verified(self, vault):
        path = vault / "notes" / "x.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "## Original\n\n## Addendum\n\nthe new content\n",
            encoding="utf-8",
        )

        exc = ObsidianPostWriteUncertain(
            "notes/x.md",
            content_hint="## Addendum",
            write_mode="insert",
        )
        assert verify_post_write(exc) == "verified"

    def test_append_mode_verified(self, vault):
        path = vault / "notes" / "x.md"
        path.parent.mkdir(parents=True)
        path.write_text("existing\n\nappended fragment\n", encoding="utf-8")

        exc = ObsidianPostWriteUncertain(
            "notes/x.md",
            content_hint="appended fragment",
            write_mode="append",
        )
        assert verify_post_write(exc) == "verified"

    def test_windows_path_separator_normalized(self, vault):
        """Bridge stores forward-slash paths; on Windows the actual
        file is at backslash-path. The helper must normalize."""
        path = vault / "notes" / "x.md"
        path.parent.mkdir(parents=True)
        path.write_text("witness here", encoding="utf-8")

        exc = ObsidianPostWriteUncertain(
            "notes\\x.md",  # backslash
            content_hint="witness",
            write_mode="insert",
        )
        assert verify_post_write(exc) == "verified"


class TestVerifyPostWriteAbsent:
    """File exists but content doesn't match, OR file doesn't exist."""

    def test_file_does_not_exist(self, vault):
        exc = ObsidianPostWriteUncertain(
            "notes/never-written.md",
            content_hint="anything",
            write_mode="insert",
        )
        assert verify_post_write(exc) == "absent"

    def test_substring_not_in_file(self, vault):
        path = vault / "notes" / "x.md"
        path.parent.mkdir(parents=True)
        path.write_text("totally different content\n", encoding="utf-8")

        exc = ObsidianPostWriteUncertain(
            "notes/x.md",
            content_hint="our addendum",
            write_mode="insert",
        )
        assert verify_post_write(exc) == "absent"

    def test_replace_sha256_mismatch(self, vault):
        path = vault / "notes" / "x.md"
        path.parent.mkdir(parents=True)
        path.write_text("file has different content", encoding="utf-8")

        exc = ObsidianPostWriteUncertain(
            "notes/x.md",
            content_hint="sha256:" + "0" * 64,  # bogus hash
            write_mode="replace",
        )
        assert verify_post_write(exc) == "absent"


class TestVerifyPostWriteIndeterminate:
    """Verify itself can't run cleanly → 'indeterminate' (caller treats
    as absent and enqueues a retry)."""

    def test_no_vault_root_in_config(self, tmp_path):
        with patch(
            "work_buddy.obsidian.post_write_verify.load_config",
            return_value={},  # no vault_root
        ):
            exc = ObsidianPostWriteUncertain(
                "notes/x.md",
                content_hint="anything",
                write_mode="insert",
            )
            assert verify_post_write(exc) == "indeterminate"

    def test_load_config_raises(self, tmp_path):
        with patch(
            "work_buddy.obsidian.post_write_verify.load_config",
            side_effect=RuntimeError("config corrupt"),
        ):
            exc = ObsidianPostWriteUncertain(
                "notes/x.md",
                content_hint="x",
                write_mode="insert",
            )
            assert verify_post_write(exc) == "indeterminate"

    def test_no_content_hint(self, vault):
        path = vault / "notes" / "x.md"
        path.parent.mkdir(parents=True)
        path.write_text("anything", encoding="utf-8")

        exc = ObsidianPostWriteUncertain(
            "notes/x.md",
            content_hint=None,  # missing — bridge should always populate
            write_mode="insert",
        )
        assert verify_post_write(exc) == "indeterminate"

    def test_read_failure(self, vault):
        """Filesystem read fails (e.g. permission denied)."""
        path = vault / "notes" / "x.md"
        path.parent.mkdir(parents=True)
        path.write_text("ok", encoding="utf-8")

        exc = ObsidianPostWriteUncertain(
            "notes/x.md", content_hint="ok", write_mode="insert",
        )
        with patch.object(
            Path, "read_text",
            side_effect=OSError("permission denied"),
        ):
            assert verify_post_write(exc) == "indeterminate"


# ---------------------------------------------------------------------------
# Integration: the real op_34ab708a.json fixture
# ---------------------------------------------------------------------------


class TestRealOpRecoverable:
    """The original op that surfaced this whole investigation:
    op_34ab708a was a vault_write_at_location call that timed out
    client-side after the addendum had been written to the Slice 1 task
    note. The file on disk has the addendum; the op record claims
    failure.

    This integration test confirms that IF this op had been processed
    by CP5, verify_post_write would have returned 'verified', and the
    gateway would have surfaced success-with-warning instead of the
    misleading 'Failed to write note' error.

    The fixture path is 'data/agents/operations/op_34ab708a.json'.
    Skipped when not present (e.g. CI).
    """

    OP_FIXTURE = (
        Path(__file__).parent.parent.parent
        / "data" / "agents" / "operations" / "op_34ab708a.json"
    )

    @pytest.mark.skipif(
        not OP_FIXTURE.exists(),
        reason="op_34ab708a.json fixture not in this environment",
    )
    def test_op_34ab708a_would_be_recovered(self):
        record = json.loads(self.OP_FIXTURE.read_text(encoding="utf-8"))
        # Sanity: this is the op we think it is.
        assert record["name"] == "vault_write_at_location"
        assert record["params"]["note"] == "tasks/notes/1d435d22-4e54-4d48-bf0d-38ecffa4fb67.md"

        # Reconstruct what the bridge would have raised: a
        # PostWriteUncertain with the inserted content as the witness.
        inserted = record["params"]["content"]
        hint = inserted[:256]  # what _make_content_hint would have produced
        exc = ObsidianPostWriteUncertain(
            record["params"]["note"],
            content_hint=hint,
            write_mode="insert",
        )

        # vault_root comes from the live config — verify_post_write reads
        # it via load_config, which here will resolve to the actual vault.
        verdict = verify_post_write(exc)
        # The Slice 1 task note has the addendum on disk (we confirmed
        # this manually during the diagnosis). So verify_post_write
        # should return "verified".
        assert verdict == "verified", (
            f"Expected 'verified' (the addendum is in the file), got "
            f"{verdict!r}. Either the file was edited / addendum removed, "
            f"or the verify logic regressed."
        )
