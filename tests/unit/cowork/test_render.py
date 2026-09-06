"""Rendering contract, with the injection boundary as the headline case.

A document's text is untrusted: agents write into documents through proposals.
The reader configuration is what stops that text from becoming executable
markup, so it is asserted directly rather than trusted.
"""

from __future__ import annotations

import shutil

import pytest

from work_buddy.cowork import render


pandoc_required = pytest.mark.skipif(
    shutil.which("pandoc") is None,
    # CI installs no pandoc, so this skip is taken on every CI run and the
    # assertions below only ever execute on a developer host that has it.
    reason="pandoc is not installed on this host",
)


def test_unknown_format_is_rejected() -> None:
    with pytest.raises(render.RenderError) as caught:
        render.render("# Doc\n", "postscript")
    assert caught.value.code == "unsupported_format"
    assert caught.value.status == 400


def test_markdown_render_is_byte_identical_and_needs_no_tool() -> None:
    """Markdown is the canonical text itself, not a conversion of it."""
    canonical = "Walters &amp; Wilder \\[Preprint\\].\n"
    assert render.render(canonical, "markdown") == canonical.encode("utf-8")


def test_oversized_documents_are_refused() -> None:
    oversized = "x" * (render.MAX_RENDERED_BYTES + 1)
    with pytest.raises(render.RenderError) as caught:
        render.render(oversized, "markdown")
    assert caught.value.code == "document_too_large"
    assert caught.value.status == 413


def test_reader_disables_every_raw_passthrough_extension() -> None:
    """The reader string is the injection boundary, so pin it explicitly."""
    for extension in ("raw_tex", "raw_attribute", "raw_html"):
        assert f"-{extension}" in render.MARKDOWN_READER


def test_pdf_uses_an_engine_that_can_set_accented_names() -> None:
    assert render.TEX_ENGINE in {"xelatex", "lualatex"}


@pandoc_required
def test_raw_latex_attribute_stays_literal_text() -> None:
    """A code span carrying {=latex} must not reach the engine as a command."""
    hostile = "Cite `\\input{/etc/passwd}`{=latex} here.\n"
    produced = render.render(hostile, "latex").decode("utf-8")
    assert "\\input{/etc/passwd}" not in produced
    assert "input" in produced  # it survives, as inert text


@pandoc_required
def test_raw_html_attribute_stays_literal_text() -> None:
    hostile = "Text `<script>alert(1)</script>`{=html} more.\n"
    produced = render.render(hostile, "html").decode("utf-8")
    assert "<script>alert(1)</script>" not in produced


@pandoc_required
def test_canonical_escaping_renders_as_the_author_wrote_it() -> None:
    """Entities and escapes decode at render time, which is why nothing strips them earlier."""
    canonical = "Abbas, M., &amp; Khan, T. I. (2024) \\[Preprint\\].\n"
    produced = render.render(canonical, "latex").decode("utf-8")
    assert "\\&" in produced
    assert "&amp;" not in produced


@pandoc_required
def test_available_formats_reports_markdown_at_minimum() -> None:
    names = [entry["format"] for entry in render.available_formats()]
    assert "markdown" in names
    assert names.index("markdown") == 0


def test_markdown_is_available_without_any_toolchain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(render, "available_tools", lambda: {"pandoc": False, "tex": False})
    names = [entry["format"] for entry in render.available_formats()]
    assert names == ["markdown"]


def test_missing_toolchain_is_a_typed_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(render, "available_tools", lambda: {"pandoc": False, "tex": False})
    with pytest.raises(render.RenderError) as caught:
        render.render("# Doc\n", "pdf")
    assert caught.value.code == "render_tool_unavailable"
    assert caught.value.status == 503
