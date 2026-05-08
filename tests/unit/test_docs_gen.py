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
