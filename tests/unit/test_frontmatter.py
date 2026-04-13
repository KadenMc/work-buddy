"""Unit tests for YAML frontmatter parsing."""

import pytest
from pathlib import Path

from work_buddy.frontmatter import parse_frontmatter, scan_frontmatter, filter_by_field


class TestParseFrontmatter:
    def test_basic_frontmatter(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("---\ntitle: Hello\nstatus: active\n---\nBody text here.")
        fm, body = parse_frontmatter(f)
        assert fm == {"title": "Hello", "status": "active"}
        assert body == "Body text here."

    def test_no_frontmatter(self, tmp_path):
        f = tmp_path / "plain.md"
        f.write_text("Just a plain markdown file.\nNo frontmatter.")
        fm, body = parse_frontmatter(f)
        assert fm == {}
        assert "Just a plain" in body

    def test_empty_frontmatter(self, tmp_path):
        f = tmp_path / "empty.md"
        f.write_text("---\n---\nBody only.")
        fm, body = parse_frontmatter(f)
        assert fm == {}
        assert body == "Body only."

    def test_malformed_yaml(self, tmp_path):
        f = tmp_path / "bad.md"
        f.write_text("---\n: invalid: yaml: [[\n---\nBody.")
        fm, body = parse_frontmatter(f)
        assert fm == {}
        assert body == "Body."

    def test_no_closing_delimiter(self, tmp_path):
        f = tmp_path / "unclosed.md"
        f.write_text("---\ntitle: Oops\nThis never closes.")
        fm, body = parse_frontmatter(f)
        assert fm == {}
        assert "---" in body or "Oops" in body

    def test_non_dict_yaml(self, tmp_path):
        f = tmp_path / "list.md"
        f.write_text("---\n- item1\n- item2\n---\nBody.")
        fm, body = parse_frontmatter(f)
        assert fm == {}

    def test_missing_file(self, tmp_path):
        f = tmp_path / "nonexistent.md"
        fm, body = parse_frontmatter(f)
        assert fm == {}
        assert body == ""


class TestScanFrontmatter:
    def test_scan_directory(self, tmp_path):
        (tmp_path / "a.md").write_text("---\nstatus: active\n---\nA")
        (tmp_path / "b.md").write_text("---\nstatus: done\n---\nB")
        (tmp_path / "c.txt").write_text("Not markdown")

        results = scan_frontmatter(tmp_path, recursive=False)
        assert len(results) == 2
        statuses = {r["status"] for r in results}
        assert statuses == {"active", "done"}

    def test_scan_with_filter(self, tmp_path):
        (tmp_path / "a.md").write_text("---\nstatus: active\n---\nA")
        (tmp_path / "b.md").write_text("---\nstatus: done\n---\nB")

        results = scan_frontmatter(
            tmp_path,
            filter_fn=lambda fm: fm.get("status") == "active",
        )
        assert len(results) == 1
        assert results[0]["status"] == "active"

    def test_scan_recursive(self, tmp_path):
        sub = tmp_path / "subdir"
        sub.mkdir()
        (tmp_path / "top.md").write_text("---\nlevel: top\n---\n")
        (sub / "nested.md").write_text("---\nlevel: nested\n---\n")

        results = scan_frontmatter(tmp_path, recursive=True)
        assert len(results) == 2
        levels = {r["level"] for r in results}
        assert levels == {"top", "nested"}


class TestFilterByField:
    def test_filter(self):
        entries = [
            {"frontmatter": {"status": "active"}, "status": "active"},
            {"frontmatter": {"status": "done"}, "status": "done"},
        ]
        filtered = filter_by_field(entries, "status", "active")
        assert len(filtered) == 1
        assert filtered[0]["status"] == "active"
