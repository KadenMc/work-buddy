"""Unit tests for the vault adapter (markdown → VaultUnit loader)."""

import pytest
from pathlib import Path

from work_buddy.knowledge.vault_adapter import load_vault_units, _extract_summary, _first_sentence, _as_list
from work_buddy.knowledge.model import VaultUnit


class TestLoadVaultUnits:
    def test_well_formed_file(self, tmp_path):
        f = tmp_path / "test-pattern.md"
        f.write_text(
            "---\n"
            "name: Test Pattern\n"
            "category: work_pattern\n"
            "severity: HIGH\n"
            "tags: [wb/metacognition, wb/work-pattern]\n"
            "last_observed: '2026-04-12'\n"
            "observation_count: 3\n"
            "---\n\n"
            "# Test Pattern\n\n"
            "## Definition\n\n"
            "A test pattern for unit testing.\n\n"
            "## Evidence\n\n"
            "* 2026-04-12 - First observation.\n"
        )
        units = load_vault_units(tmp_path)
        assert len(units) == 1
        u = units["personal/test-pattern"]
        assert isinstance(u, VaultUnit)
        assert u.name == "Test Pattern"
        assert u.category == "work_pattern"
        assert u.severity == "HIGH"
        assert u.observation_count == 3
        assert u.source_file == "test-pattern.md"
        assert u.kind == "personal"
        assert u.scope == "personal"

    def test_subdirectory_path_generation(self, tmp_path):
        sub = tmp_path / "work_patterns"
        sub.mkdir()
        f = sub / "example-pattern-b.md"
        f.write_text(
            "---\nname: Example Pattern B\ncategory: work_pattern\n---\n\nBody.\n"
        )
        units = load_vault_units(tmp_path)
        assert "personal/work_patterns/example-pattern-b" in units

    def test_missing_frontmatter_skipped(self, tmp_path):
        f = tmp_path / "no-fm.md"
        f.write_text("Just a plain file with no frontmatter.")
        units = load_vault_units(tmp_path)
        assert len(units) == 0

    def test_missing_name_skipped(self, tmp_path):
        f = tmp_path / "no-name.md"
        f.write_text("---\ncategory: feedback\n---\n\nHas frontmatter but no name.\n")
        units = load_vault_units(tmp_path)
        assert len(units) == 0

    def test_missing_directory_returns_empty(self):
        units = load_vault_units(Path("/nonexistent/path"))
        assert units == {}

    def test_summary_from_definition_section(self, tmp_path):
        f = tmp_path / "with-def.md"
        f.write_text(
            "---\nname: Defined\n---\n\n"
            "# Defined\n\n"
            "## Definition\n\n"
            "This is the definition paragraph.\n\n"
            "## Evidence\n\nSome evidence.\n"
        )
        units = load_vault_units(tmp_path)
        u = units["personal/with-def"]
        assert u.content["summary"] == "This is the definition paragraph."

    def test_summary_from_first_paragraph(self, tmp_path):
        f = tmp_path / "no-def.md"
        f.write_text(
            "---\nname: No Def\n---\n\n"
            "First paragraph of content.\n\n"
            "Second paragraph.\n"
        )
        units = load_vault_units(tmp_path)
        u = units["personal/no-def"]
        assert u.content["summary"] == "First paragraph of content."

    def test_context_chains_loaded(self, tmp_path):
        f = tmp_path / "chained.md"
        f.write_text(
            "---\n"
            "name: Chained\n"
            "context_before: [dev/dev-mode]\n"
            "context_after: [dev/extra]\n"
            "---\n\nBody.\n"
        )
        units = load_vault_units(tmp_path)
        u = units["personal/chained"]
        assert u.context_before == ["dev/dev-mode"]
        assert u.context_after == ["dev/extra"]

    def test_comma_separated_tags(self, tmp_path):
        f = tmp_path / "tagged.md"
        f.write_text(
            "---\nname: Tagged\ntags: wb/meta, wb/test\n---\n\nBody.\n"
        )
        units = load_vault_units(tmp_path)
        u = units["personal/tagged"]
        assert u.tags == ["wb/meta", "wb/test"]


class TestExtractSummary:
    def test_definition_section(self):
        body = "# Title\n\n## Definition\n\nDef text here.\n\n## Other\n\nStuff."
        assert _extract_summary(body) == "Def text here."

    def test_first_paragraph_fallback(self):
        body = "First paragraph.\n\nSecond paragraph."
        assert _extract_summary(body) == "First paragraph."

    def test_skips_headings(self):
        body = "# Heading\n\nActual content.\n\nMore."
        assert _extract_summary(body) == "Actual content."

    def test_empty_body(self):
        assert _extract_summary("") == ""


class TestFirstSentence:
    def test_simple(self):
        assert _first_sentence("Hello world. More stuff.") == "Hello world."

    def test_no_period(self):
        text = "A" * 130
        assert len(_first_sentence(text)) == 120

    def test_empty(self):
        assert _first_sentence("") == ""


class TestAsList:
    def test_none(self):
        assert _as_list(None) == []

    def test_list_passthrough(self):
        assert _as_list(["a", "b"]) == ["a", "b"]

    def test_comma_string(self):
        assert _as_list("a, b, c") == ["a", "b", "c"]

    def test_single_string(self):
        assert _as_list("single") == ["single"]
