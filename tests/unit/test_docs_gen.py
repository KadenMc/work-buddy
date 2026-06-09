"""Unit tests for the docs handbook generator.

These tests lock the contract between the kind taxonomy and the renderer
dispatcher: every registered renderer must succeed on a minimal instance
of its kind, and unknown kinds must fall back to ``_render_generic`` —
which only touches base ``KnowledgeUnit`` fields and therefore cannot
``AttributeError``.

The live-store smoke test guards against the original CI failure:
``generate_docs(write=False)`` against the real ``knowledge/store/``
must never raise.
"""

import pytest

from work_buddy.knowledge import docs_gen
from work_buddy.knowledge.docs_gen import (
    _render_generic,
    _render_unit,
    _RENDERERS,
    generate_docs,
)
from work_buddy.knowledge.model import (
    CapabilityUnit,
    ConceptUnit,
    DirectionsUnit,
    IntegrationUnit,
    PromptUnit,
    ReferenceUnit,
    ServiceUnit,
    SystemUnit,
    WorkflowUnit,
)


def _minimal(cls, **extra):
    """Build a minimal instance of any PromptUnit subclass for render testing."""
    base = dict(
        path=f"test/{cls.__name__.lower()}",
        name=f"Test {cls.__name__}",
        description="A minimal fixture",
        content={"full": "Some prose."},
    )
    base.update(extra)
    return cls(**base)


class TestRenderEachKind:
    """Every kind must render to non-empty markdown without exception."""

    def test_directions_renders(self):
        u = _minimal(
            DirectionsUnit,
            trigger="when X happens",
            command="wb-test",
            workflow="test/wf",
            capabilities=["cap_a"],
        )
        out = _render_unit(u)
        assert out.startswith("# Test DirectionsUnit")
        assert "when X happens" in out
        assert "wb-test" in out

    def test_capability_renders(self):
        u = _minimal(
            CapabilityUnit,
            capability_name="task_create",
            category="tasks",
            parameters={"name": {"type": "str", "required": True, "description": "Task name"}},
        )
        out = _render_unit(u)
        assert "task_create" in out
        assert "## Parameters" in out

    def test_workflow_renders(self):
        u = _minimal(
            WorkflowUnit,
            workflow_name="task-triage",
            execution="main",
            steps=[{"id": "s1", "name": "Step", "step_type": "reasoning", "depends_on": []}],
            step_instructions={"s1": "Do the thing."},
        )
        out = _render_unit(u)
        assert "task-triage" in out
        assert "## Steps" in out

    def test_system_renders_via_generic(self):
        u = _minimal(SystemUnit)
        out = _render_unit(u)
        assert out.startswith("# Test SystemUnit")
        assert "## Details" in out
        # Narrowed SystemUnit has no ports/entry_points; output must not reference them.
        assert "## Ports" not in out
        assert "## Entry points" not in out

    def test_service_renders_via_generic(self):
        u = _minimal(
            ServiceUnit,
            ports=[5127],
            health_url="/health",
            entry_points=["m:main"],
        )
        out = _render_unit(u)
        assert out.startswith("# Test ServiceUnit")
        # Specialized service-template (port banner / health URL) is deferred —
        # generic renderer should not surface them yet.
        assert "5127" not in out
        assert "/health" not in out

    def test_integration_renders_via_generic(self):
        u = _minimal(
            IntegrationUnit,
            external_system="Obsidian",
            bridge_module="work_buddy.obsidian.bridge",
            ports=[27125],
        )
        out = _render_unit(u)
        assert out.startswith("# Test IntegrationUnit")
        assert "Obsidian" not in out  # generic renderer ignores kind-specific fields

    def test_reference_renders_via_generic(self):
        u = _minimal(ReferenceUnit, entry_points=["work_buddy.x.y"])
        out = _render_unit(u)
        assert out.startswith("# Test ReferenceUnit")
        assert "work_buddy.x.y" not in out  # deferred to a specialized renderer

    def test_concept_renders_via_generic(self):
        u = _minimal(ConceptUnit)
        out = _render_unit(u)
        assert out.startswith("# Test ConceptUnit")
        assert "Some prose." in out


class TestUnknownKindFallback:
    """Unknown kinds must dispatch to _render_generic without AttributeError.

    A bare PromptUnit carries no kind-specific fields, so a renderer that
    assumes a typed subclass would crash on attribute access. The dispatcher
    must route any kind not in _RENDERERS to the generic template, which
    only touches base KnowledgeUnit fields.
    """

    def test_unknown_kind_uses_generic(self):
        u = PromptUnit(
            path="ad/hoc",
            kind="made-up",
            name="Ad hoc",
            description="Unknown kind",
            content={"full": "Body"},
        )
        out = _render_unit(u)
        assert out.startswith("# Ad hoc")
        assert "Body" in out

    def test_bare_prompt_unit_renders_without_attributeerror(self):
        """Any PromptUnit with no typed-subclass fields must render cleanly
        through the generic template. Catches the class of bug where a
        renderer accesses a kind-specific field on a wrong-shape object."""
        u = PromptUnit(
            path="ad/hoc-2",
            kind="some-other-kind",
            name="Bare",
            description="A bare PromptUnit",
            content={"full": "x"},
        )
        out = _render_unit(u)
        assert out


class TestGenericRendererSurface:
    """``_render_generic`` must only touch base ``KnowledgeUnit`` fields,
    so it stays safe against any future ad-hoc kind."""

    def test_generic_does_not_emit_port_section(self):
        u = PromptUnit(
            path="x", kind="x", name="X", description="x",
            content={"full": "body"},
        )
        out = _render_generic(u)
        assert "## Ports" not in out
        assert "## Entry points" not in out

    def test_generic_emits_children_when_present(self):
        u = PromptUnit(
            path="x", kind="x", name="X", description="x",
            children=["x/a", "x/b"],
        )
        out = _render_generic(u)
        assert "## Children" in out
        assert "x_a.md" in out
        assert "x_b.md" in out

    def test_generic_emits_requires_when_present(self):
        u = PromptUnit(
            path="x", kind="x", name="X", description="x",
            requires=["dep_a"],
        )
        out = _render_generic(u)
        assert "## Requirements" in out
        assert "dep_a" in out


class TestRendererRegistry:
    def test_no_render_system_in_registry(self):
        """The dangerous _render_system fallback was retired. Registry
        should hold renderers only for kinds with structured templates."""
        assert "system" not in _RENDERERS
        assert set(_RENDERERS.keys()) == {"directions", "capability", "workflow"}


class TestLiveStoreSmoke:
    """Regression guard for the original CI crash. ``generate_docs(write=False)``
    must succeed against the real knowledge store regardless of how units
    are currently classified."""

    def test_generate_docs_dry_run_succeeds(self):
        result = generate_docs(write=False)
        assert isinstance(result, dict)
        assert result["pages"] > 0
        assert result["units"] > 0


class TestNavInjection:
    """``_write_nav_to_mkdocs`` splices the generated nav between the marker
    comments. The committed block is intentionally minimal (drift-proofing): the
    full nav is rebuilt at build time, never tracked in git. So the splice must
    fill a marker-only block, preserve everything outside the markers (config
    above, anything below), and be idempotent.
    """

    # A marker block that is NOT at end of file — there is trailing config the
    # splice must not eat. The latent bug this guards against dropped it.
    _MKDOCS = (
        "site_name: Test\n"
        "theme:\n"
        "  name: material\n"
        "\n"
        "# AUTOGEN_NAV_START\n"
        "nav:\n"
        "- Home: index.md\n"
        "# AUTOGEN_NAV_END\n"
        "\n"
        "# trailing config the splice must not eat\n"
        "extra:\n"
        "  sentinel: keep-me\n"
    )

    def _prepare(self, tmp_path, monkeypatch, text=None):
        (tmp_path / "mkdocs.yml").write_text(text or self._MKDOCS, encoding="utf-8")
        monkeypatch.setattr(docs_gen, "_REPO_ROOT", tmp_path)

    def test_fills_block_and_preserves_surroundings(self, tmp_path, monkeypatch):
        import yaml

        self._prepare(tmp_path, monkeypatch)
        docs_gen._write_nav_to_mkdocs([{"Tasks": [{"Triage": "handbook/tasks_triage.md"}]}])
        out = (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")

        # Hand-written config above AND below the marker block survives.
        assert "site_name: Test" in out
        assert "# trailing config the splice must not eat" in out
        assert "sentinel: keep-me" in out
        # Both markers survive.
        assert "# AUTOGEN_NAV_START" in out
        assert "# AUTOGEN_NAV_END" in out

        # The generated nav landed and parses as valid YAML.
        loaded = yaml.safe_load(out)
        # _write_nav_to_mkdocs always prepends Home + Handbook to the sections.
        assert loaded["nav"][0] == {"Home": "index.md"}
        assert loaded["nav"][1] == {"Handbook": "handbook/index.md"}
        assert {"Tasks": [{"Triage": "handbook/tasks_triage.md"}]} in loaded["nav"]
        # Trailing config is still structurally present, not just textually.
        assert loaded["extra"]["sentinel"] == "keep-me"

    def test_marker_only_block_is_tolerated(self, tmp_path, monkeypatch):
        # Even with nothing between the markers, the fill must succeed.
        bare = "site_name: Test\n# AUTOGEN_NAV_START\n# AUTOGEN_NAV_END\n"
        self._prepare(tmp_path, monkeypatch, text=bare)
        docs_gen._write_nav_to_mkdocs([{"Tasks": [{"Triage": "handbook/tasks_triage.md"}]}])
        out = (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")

        import yaml

        loaded = yaml.safe_load(out)
        assert loaded["nav"][0] == {"Home": "index.md"}
        assert loaded["site_name"] == "Test"

    def test_idempotent(self, tmp_path, monkeypatch):
        self._prepare(tmp_path, monkeypatch)
        nav = [{"Tasks": [{"Triage": "handbook/tasks_triage.md"}]}]
        docs_gen._write_nav_to_mkdocs(nav)
        once = (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")
        docs_gen._write_nav_to_mkdocs(nav)
        twice = (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")
        assert once == twice
