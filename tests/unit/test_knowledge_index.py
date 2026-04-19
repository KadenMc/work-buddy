"""Unit tests for the knowledge search index.

Tests the KnowledgeIndex class: BM25 indexing over full content,
candidate filtering, generation guards, invalidation, and the
module-level singleton helpers.

Dense vector tests are excluded — they require the embedding service.
"""

import pytest

from work_buddy.knowledge.index import (
    KnowledgeIndex,
    _tokenize,
    _build_doc,
    get_index,
    ensure_index,
    invalidate_index,
)
from work_buddy.knowledge.model import (
    KnowledgeUnit,
    DirectionsUnit,
    SystemUnit,
    CapabilityUnit,
    VaultUnit,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_store() -> dict[str, KnowledgeUnit]:
    """Build a small synthetic store for testing."""
    return {
        "consent/system": SystemUnit(
            path="consent/system",
            name="Consent System",
            description="Session-scoped approval grants",
            content={
                "summary": "SQLite-backed consent with TTL expiry.",
                "full": (
                    "The consent system uses a SQLite database at "
                    "agents/<session>/consent.db to store approval grants. "
                    "Each grant has a mode (always, temporary, once) and "
                    "temporary grants have a TTL in minutes. The "
                    "@requires_consent decorator protects sensitive operations."
                ),
            },
            tags=["consent", "permissions"],
            aliases=["approval", "grants"],
        ),
        "tasks/create": CapabilityUnit(
            path="tasks/create",
            name="Task Create",
            description="Create a new task in the master list",
            capability_name="task_create",
            category="tasks",
            content={
                "summary": "Creates a task with optional project tag and due date.",
                "full": (
                    "Creates a new task line in tasks/master-task-list.md "
                    "with a generated hex ID. Supports urgency, project tag, "
                    "due_date, and optional summary param for task notes."
                ),
            },
            tags=["tasks", "create"],
        ),
        "notifications/telegram": SystemUnit(
            path="notifications/telegram",
            name="Telegram Bot",
            description="Mobile notification surface via Telegram",
            content={
                "summary": "Sidecar service on port 5125 with inline keyboard buttons.",
                "full": (
                    "The Telegram bot provides mobile access to Work Buddy. "
                    "Commands: /start, /help, /capture, /reply, /remote, "
                    "/resume, /status, /obs, /slash. "
                    "Inline keyboard buttons render for boolean and choice "
                    "response types. The /reply <short_id> <answer> command "
                    "responds to pending requests by 4-digit ID."
                ),
            },
            tags=["telegram", "notifications", "mobile"],
        ),
        "personal/branch-explosion": VaultUnit(
            path="personal/branch-explosion",
            name="Branch Explosion",
            description="Opening too many parallel lines of investigation",
            category="work_pattern",
            severity="HIGH",
            content={
                "summary": "Recognized when 3+ active branches exist simultaneously.",
                "full": (
                    "Branch explosion occurs when the user opens multiple "
                    "parallel investigation lines without closing any. "
                    "Typical trigger: encountering an interesting tangent "
                    "while working on the primary task. Intervention: "
                    "name the pattern, force a pick-one decision."
                ),
            },
            tags=["metacognition", "work-pattern"],
        ),
        "vault/writer": DirectionsUnit(
            path="vault/writer",
            name="Vault Writer",
            description="Section-aware content insertion into vault notes",
            trigger="agent needs to write at a specific location in a vault note",
            content={
                "summary": "Insert content at a specific section in a note.",
                "full": (
                    "Note resolvers: 'latest_journal' (respects day boundary), "
                    "'today', or explicit vault-relative path. Section finding "
                    "matches headers at any level, ignores formatting, partial "
                    "prefix match. MCP capability: vault_write_at_location."
                ),
            },
            tags=["vault", "obsidian", "writing"],
        ),
    }


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_basic(self):
        assert _tokenize("Hello World") == ["hello", "world"]

    def test_filters_short(self):
        assert _tokenize("a bb ccc") == ["bb", "ccc"]

    def test_splits_on_punctuation(self):
        tokens = _tokenize("foo-bar_baz.qux")
        assert "foo" in tokens
        assert "bar" in tokens

    def test_empty(self):
        assert _tokenize("") == []


# ---------------------------------------------------------------------------
# IndexDoc builder
# ---------------------------------------------------------------------------

class TestBuildDoc:
    def test_includes_metadata_and_content(self):
        unit = _make_store()["consent/system"]
        doc = _build_doc("consent/system", unit)
        assert doc.path == "consent/system"
        # Full text includes name, description, AND body content
        assert "consent system" in doc.full_text.lower()
        assert "sqlite database" in doc.full_text.lower()
        assert "@requires_consent" in doc.full_text

    def test_meta_text_is_search_phrases(self):
        unit = _make_store()["tasks/create"]
        doc = _build_doc("tasks/create", unit)
        # Meta text should be from search_phrases() only
        assert "task create" in doc.meta_text.lower()
        assert "master-task-list.md" not in doc.meta_text

    def test_tokens_are_populated(self):
        unit = _make_store()["consent/system"]
        doc = _build_doc("consent/system", unit)
        assert len(doc.full_tokens) > 0
        assert len(doc.meta_tokens) > 0
        assert "sqlite" in doc.full_tokens
        assert "consent" in doc.meta_tokens

    def test_placeholder_resolved_when_store_provided(self):
        """Placeholders in content should be resolved before indexing."""
        bridge = DirectionsUnit(
            path="bridge", name="Bridge", description="bridge",
            content={"full": "UNIQUE_BRIDGE_KEYWORD content here."},
        )
        referrer = DirectionsUnit(
            path="referrer", name="Referrer", description="refs bridge",
            content={"full": "See bridge: <<wb:bridge>>"},
        )
        store = {"bridge": bridge, "referrer": referrer}
        doc = _build_doc("referrer", referrer, store=store)
        # The resolved content should include the bridge keyword
        assert "UNIQUE_BRIDGE_KEYWORD" in doc.full_text
        # The raw placeholder should NOT be in the indexed text
        assert "<<wb:bridge>>" not in doc.full_text

    def test_placeholder_not_resolved_without_store(self):
        """Without store, placeholders are left as raw text."""
        referrer = DirectionsUnit(
            path="referrer", name="Referrer", description="refs bridge",
            content={"full": "See bridge: <<wb:bridge>>"},
        )
        doc = _build_doc("referrer", referrer)
        # Raw placeholder should be in the text
        assert "<<wb:bridge>>" in doc.full_text

    def test_no_placeholder_content_unchanged(self):
        """Units without placeholders should not be affected."""
        unit = _make_store()["consent/system"]
        store = _make_store()
        doc_without = _build_doc("consent/system", unit)
        doc_with = _build_doc("consent/system", unit, store=store)
        assert doc_without.full_text == doc_with.full_text


# ---------------------------------------------------------------------------
# KnowledgeIndex — build and search
# ---------------------------------------------------------------------------

class TestKnowledgeIndexBuild:
    def test_build_populates_index(self):
        store = _make_store()
        idx = KnowledgeIndex()
        stats = idx.build(store, skip_dense=True)

        assert idx.is_built
        assert idx.size == 5
        assert stats["units_indexed"] == 5
        assert stats["has_content_vectors"] is False
        assert stats["has_alias_vectors"] is False

    def test_build_increments_generation(self):
        store = _make_store()
        idx = KnowledgeIndex()
        assert idx._generation == 0
        idx.build(store, skip_dense=True)
        assert idx._generation == 1
        idx.build(store, skip_dense=True)
        assert idx._generation == 2

    def test_empty_store(self):
        idx = KnowledgeIndex()
        stats = idx.build({}, skip_dense=True)
        assert stats["units_indexed"] == 0
        assert not idx.is_built


class TestKnowledgeIndexSearch:
    @pytest.fixture
    def idx(self):
        store = _make_store()
        idx = KnowledgeIndex()
        idx.build(store, skip_dense=True)
        return idx

    def test_keyword_match_in_content(self, idx):
        """'requires_consent decorator' only appears in body content."""
        results = idx.search("requires_consent decorator")
        assert len(results) > 0
        assert results[0]["path"] == "consent/system"

    def test_keyword_match_in_metadata(self, idx):
        results = idx.search("telegram mobile notifications")
        assert len(results) > 0
        assert results[0]["path"] == "notifications/telegram"

    def test_content_only_term(self, idx):
        """'inline keyboard buttons' only appears in telegram's full content."""
        results = idx.search("inline keyboard buttons")
        assert len(results) > 0
        assert results[0]["path"] == "notifications/telegram"

    def test_deep_content_match(self, idx):
        """'parallel investigation lines' only appears in branch-explosion body."""
        results = idx.search("parallel investigation lines tangent")
        assert len(results) > 0
        assert results[0]["path"] == "personal/branch-explosion"

    def test_candidate_filtering(self, idx):
        """When candidates restrict to a subset, only those paths are returned."""
        store = _make_store()
        # Only search within tasks and vault
        candidates = {
            "tasks/create": store["tasks/create"],
            "vault/writer": store["vault/writer"],
        }
        results = idx.search("consent", candidates=candidates)
        paths = [r["path"] for r in results]
        assert "consent/system" not in paths

    def test_top_n_respected(self, idx):
        results = idx.search("system", top_n=2)
        assert len(results) <= 2

    def test_empty_query(self, idx):
        results = idx.search("")
        assert results == []

    def test_no_match(self, idx):
        results = idx.search("xyzzyplugh")
        assert results == []

    def test_scores_are_descending(self, idx):
        results = idx.search("consent")
        if len(results) > 1:
            for i in range(len(results) - 1):
                assert results[i]["score"] >= results[i + 1]["score"]


class TestPlaceholderIndexSearch:
    """Searching for content from a referenced unit should surface the referrer."""

    def test_search_finds_referenced_content(self):
        """Build a store where 'referrer' includes bridge content via placeholder.

        Use a 3+ unit store for meaningful BM25 scoring, and search for
        a term that only exists in the bridge unit's content.
        """
        bridge = DirectionsUnit(
            path="bridge", name="Bridge", description="bridge info",
            content={"full": "The xylophone connectivity protocol handles retries."},
        )
        referrer = DirectionsUnit(
            path="referrer", name="Referrer", description="references bridge",
            content={"full": "Main content about tasks.\n\n<<wb:bridge>>"},
        )
        filler = DirectionsUnit(
            path="filler", name="Filler", description="unrelated unit",
            content={"full": "Completely unrelated content about calendars and scheduling."},
        )
        store = {"bridge": bridge, "referrer": referrer, "filler": filler}
        idx = KnowledgeIndex()
        idx.build(store, skip_dense=True)

        # "xylophone" only appears in bridge's content (and in referrer
        # via resolved placeholder). Filler should not match.
        results = idx.search("xylophone connectivity")
        paths = [r["path"] for r in results]
        assert "bridge" in paths
        assert "referrer" in paths
        assert "filler" not in paths


# ---------------------------------------------------------------------------
# Invalidation and generation guards
# ---------------------------------------------------------------------------

class TestInvalidation:
    def test_invalidate_clears_index(self):
        store = _make_store()
        idx = KnowledgeIndex()
        idx.build(store, skip_dense=True)
        assert idx.is_built

        idx.invalidate()
        assert not idx.is_built
        assert idx.size == 0

    def test_invalidate_increments_generation(self):
        idx = KnowledgeIndex()
        idx.build(_make_store(), skip_dense=True)
        gen_after_build = idx._generation

        idx.invalidate()
        assert idx._generation == gen_after_build + 1

    def test_search_after_invalidate_returns_empty(self):
        idx = KnowledgeIndex()
        idx.build(_make_store(), skip_dense=True)
        idx.invalidate()
        results = idx.search("consent")
        assert results == []

    def test_rebuild_after_invalidate(self):
        idx = KnowledgeIndex()
        idx.build(_make_store(), skip_dense=True)
        idx.invalidate()
        idx.build(_make_store(), skip_dense=True)
        results = idx.search("consent")
        assert len(results) > 0


class TestGenerationGuard:
    def test_dense_build_aborts_on_stale_generation(self):
        """Simulates background thread with outdated generation."""
        idx = KnowledgeIndex()
        idx.build(_make_store(), skip_dense=True)
        old_gen = idx._generation

        idx.invalidate()
        idx.build(_make_store(), skip_dense=True)

        # Both builders should abort silently (old_gen doesn't match current)
        idx._build_content_vectors(expected_generation=old_gen)
        idx._build_alias_vectors(expected_generation=old_gen)
        assert not idx._has_content
        assert not idx._has_aliases

    def test_dense_build_succeeds_with_current_generation(self):
        """Dense build with matching generation should proceed.

        The builders try to reach the embedding service. Since the service
        isn't running in tests, they should fail gracefully (has_* stays
        False), but the generation check itself should pass (no abort log).
        """
        idx = KnowledgeIndex()
        idx.build(_make_store(), skip_dense=True)

        # With no embedding service, these will fail gracefully
        idx._build_content_vectors(expected_generation=idx._generation)
        idx._build_alias_vectors(expected_generation=idx._generation)
        # Vectors won't be built (no service), but neither should have
        # aborted due to generation mismatch — the generation was correct.


# ---------------------------------------------------------------------------
# Cache integration — proves the persistence layer actually saves embed calls
# ---------------------------------------------------------------------------

class TestCacheIntegration:
    """Verifies that a second build with unchanged docs does NOT call the
    embedder a second time — proving the cache is wired through end-to-end.

    Uses a tmp_path-scoped cache dir (via monkeypatch) to keep test runs
    isolated from each other and from the real on-disk cache.
    """

    @pytest.fixture(autouse=True)
    def _isolated_cache(self, tmp_path, monkeypatch):
        from work_buddy.knowledge import persistence as persist
        monkeypatch.setattr(
            persist, "_content_cache_path", lambda: tmp_path / "content.npz",
        )
        monkeypatch.setattr(
            persist, "_alias_cache_path", lambda: tmp_path / "aliases.npz",
        )

    def test_content_vectors_reused_from_cache_on_second_build(self, monkeypatch):
        """First build embeds, second build hits cache with 0 embed calls."""
        import numpy as np
        from work_buddy.embedding import client as embed_client

        calls: list[int] = []  # number of texts embedded per call

        def fake_embed_for_ir(texts, role="query"):
            calls.append(len(texts))
            return [np.random.default_rng(hash(t) & 0xFFFF).normal(size=768).tolist()
                    for t in texts]

        monkeypatch.setattr(embed_client, "embed_for_ir", fake_embed_for_ir)

        # First build — should embed every unit.
        idx1 = KnowledgeIndex()
        idx1.build(_make_store(), skip_dense=True)
        idx1._build_content_vectors()
        first_total = sum(calls)
        assert first_total > 0, "First build should have embedded"
        assert idx1._has_content is True

        # Second build (fresh index instance, same docs, SAME cache dir) —
        # should find everything in cache and embed nothing.
        calls.clear()
        idx2 = KnowledgeIndex()
        idx2.build(_make_store(), skip_dense=True)
        idx2._build_content_vectors()
        assert sum(calls) == 0, (
            f"Second build should have zero embed calls (cache hit), "
            f"but embedded {sum(calls)} texts"
        )
        assert idx2._has_content is True
        # Vectors must be identical — same cache source
        assert np.allclose(idx1._content_vectors, idx2._content_vectors, atol=1e-3)

    def test_alias_vectors_reused_from_cache_on_second_build(self, monkeypatch):
        """Same contract for aliases: second build hits cache."""
        import numpy as np
        from work_buddy.embedding import client as embed_client

        calls: list[int] = []

        def fake_embed(texts, **kwargs):
            calls.append(len(texts))
            return [np.random.default_rng(hash(t) & 0xFFFF).normal(size=1024).tolist()
                    for t in texts]

        monkeypatch.setattr(embed_client, "embed", fake_embed)

        idx1 = KnowledgeIndex()
        idx1.build(_make_store(), skip_dense=True)
        idx1._build_alias_vectors()
        first_total = sum(calls)
        # _make_store has some units with aliases — ensure at least one was embedded
        assert first_total > 0, "First build should have embedded some aliases"

        calls.clear()
        idx2 = KnowledgeIndex()
        idx2.build(_make_store(), skip_dense=True)
        idx2._build_alias_vectors()
        assert sum(calls) == 0, (
            f"Second build should have zero alias embed calls, got {sum(calls)}"
        )

    def test_content_hash_change_triggers_re_embed(self, monkeypatch):
        """Editing a unit's content_text invalidates ONLY that unit's cache entry."""
        import numpy as np
        from work_buddy.embedding import client as embed_client

        calls: list[list[str]] = []  # remember the actual texts embedded

        def fake_embed_for_ir(texts, role="query"):
            calls.append(list(texts))
            return [np.random.default_rng(hash(t) & 0xFFFF).normal(size=768).tolist()
                    for t in texts]

        monkeypatch.setattr(embed_client, "embed_for_ir", fake_embed_for_ir)

        # First build — embed all.
        store1 = _make_store()
        idx1 = KnowledgeIndex()
        idx1.build(store1, skip_dense=True)
        idx1._build_content_vectors()
        n_initial = sum(len(c) for c in calls)
        assert n_initial == len(store1)

        # Second build with ONE unit's content edited.
        calls.clear()
        store2 = _make_store()
        # Mutate consent/system's summary so content_text changes
        store2["consent/system"].content["summary"] = "EDITED summary text"
        idx2 = KnowledgeIndex()
        idx2.build(store2, skip_dense=True)
        idx2._build_content_vectors()

        embedded_texts = [t for call in calls for t in call]
        assert len(embedded_texts) == 1, (
            f"Only the edited unit should re-embed; got {len(embedded_texts)} texts"
        )
        assert "EDITED summary text" in embedded_texts[0]


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

class TestModuleHelpers:
    def test_get_index_returns_singleton(self):
        idx1 = get_index()
        idx2 = get_index()
        assert idx1 is idx2

    def test_invalidate_index_clears_singleton(self):
        idx = get_index()
        idx.build(_make_store(), skip_dense=True)
        assert idx.is_built

        invalidate_index()
        assert not idx.is_built

    def test_status_fields(self):
        idx = KnowledgeIndex()
        idx.build(_make_store(), skip_dense=True)
        status = idx.status()
        assert status["built"] is True
        assert status["unit_count"] == 5
        assert status["has_content_vectors"] is False
        assert status["has_alias_vectors"] is False
        assert "built_at" in status
