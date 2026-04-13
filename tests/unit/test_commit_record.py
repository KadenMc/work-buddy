"""Unit tests for commit_record artifact type and storage."""

import json

import pytest

from work_buddy.artifacts import ArtifactStore, ARTIFACT_TYPES


@pytest.mark.unit
class TestCommitArtifactType:
    def test_commit_type_registered(self):
        assert "commit" in ARTIFACT_TYPES

    def test_commit_ttl_is_90_days(self):
        assert ARTIFACT_TYPES["commit"] == 90


@pytest.mark.unit
class TestCommitRecordStorage:
    @pytest.fixture
    def store(self, tmp_path):
        return ArtifactStore(data_root=tmp_path)

    def test_saves_with_correct_structure(self, store):
        record = {
            "commit_hash": "abc1234",
            "message": "Add feature X",
            "branch": "main",
            "files_changed": ["work_buddy/foo.py"],
            "tests": {
                "files_run": ["tests/unit/test_foo.py"],
                "passed": 3,
                "failed": 0,
            },
            "knowledge_units_updated": [],
            "summary": "Added feature X",
        }
        rec = store.save(
            content=json.dumps(record),
            type="commit",
            slug="commit-abc1234",
            ext="json",
            tags=["commit", "main"],
            description="Added feature X",
        )
        assert rec.type == "commit"
        assert rec.ext == "json"
        assert "commit" in rec.tags

        content = json.loads(rec.path.read_text(encoding="utf-8"))
        assert content["commit_hash"] == "abc1234"
        assert content["files_changed"] == ["work_buddy/foo.py"]
        assert content["tests"]["passed"] == 3

    def test_slug_contains_hash_prefix(self, store):
        rec = store.save(
            content=json.dumps({"commit_hash": "deadbeef123"}),
            type="commit",
            slug="commit-deadbee",
            ext="json",
        )
        assert "deadbee" in rec.slug

    def test_lists_only_commit_type(self, store):
        store.save(content="{}", type="commit", slug="commit-aaa", ext="json")
        store.save(content="{}", type="commit", slug="commit-bbb", ext="json")
        store.save(content="x", type="scratch", slug="unrelated", ext="txt")

        results = store.list(type="commit")
        assert len(results) == 2
        assert all(r.type == "commit" for r in results)

    def test_inherits_90_day_ttl(self, store):
        rec = store.save(content="{}", type="commit", slug="commit-ccc", ext="json")
        assert rec.ttl_days == 90

    def test_minimal_record(self, store):
        record = {
            "commit_hash": "ddd4444",
            "message": "Fix typo",
        }
        rec = store.save(
            content=json.dumps(record),
            type="commit",
            slug="commit-ddd4444",
            ext="json",
            tags=["commit"],
            description="Fix typo",
        )
        content = json.loads(rec.path.read_text(encoding="utf-8"))
        assert content["commit_hash"] == "ddd4444"
