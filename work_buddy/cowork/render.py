"""Convert a Co-work document's canonical Markdown into a deliverable file.

Rendering runs entirely on the server so one implementation owns the conversion
for every format. Markdown needs no external program; every other format shells
out to pandoc, and PDF additionally needs a TeX engine.

## Document content is untrusted input

Agents write into documents through proposals, so a document can carry whatever
an agent was persuaded to write. Handed to pandoc's default reader, a code span
carrying a raw-format attribute becomes executable markup: ``{=latex}`` reaches
the TeX engine as a real command, ``{=html}`` reaches the browser as a real tag.
A TeX engine configured to read freely will then embed any file it is pointed at.

Two properties keep that shut, and both are load-bearing rather than defensive
decoration:

- The reader never interprets raw passthrough. Raw TeX, raw HTML, and raw
  attributes are disabled, so those constructs stay literal text.
- The canonical projection's own backslash escaping is preserved end to end. No
  step here may "clean up" the escaping it receives, because that escaping is
  what keeps a document's literal punctuation literal.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from work_buddy.compat import subprocess_creation_flags
from work_buddy.cowork.materialization import MAX_RENDERED_BYTES


RENDER_TIMEOUT_SECONDS = 120

# Raw passthrough is what turns document text into executable markup, so the
# reader is pinned rather than left to pandoc's default.
MARKDOWN_READER = "markdown-raw_tex-raw_attribute-raw_html"

# pdflatex cannot set the accented names ordinary prose carries.
TEX_ENGINE = "xelatex"


class RenderError(Exception):
    """A render failed in a way the caller should surface, not retry blindly."""

    def __init__(self, code: str, message: str, *, status: int = 500) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class RenderFormat:
    """One deliverable shape, and what the host must provide to produce it."""

    name: str
    extension: str
    media_type: str
    requires: tuple[str, ...]
    label: str


FORMATS: dict[str, RenderFormat] = {
    "markdown": RenderFormat(
        name="markdown",
        extension=".md",
        media_type="text/markdown; charset=utf-8",
        requires=(),
        label="Markdown",
    ),
    "html": RenderFormat(
        name="html",
        extension=".html",
        media_type="text/html; charset=utf-8",
        requires=("pandoc",),
        label="HTML",
    ),
    "latex": RenderFormat(
        name="latex",
        extension=".tex",
        media_type="application/x-tex; charset=utf-8",
        requires=("pandoc",),
        label="LaTeX",
    ),
    "docx": RenderFormat(
        name="docx",
        extension=".docx",
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        requires=("pandoc",),
        label="Word",
    ),
    "pdf": RenderFormat(
        name="pdf",
        extension=".pdf",
        media_type="application/pdf",
        requires=("pandoc", "tex"),
        label="PDF",
    ),
}


def _probe(command: list[str]) -> bool:
    """Run a version probe, because a resolvable name is not a working program.

    An installer stub resolves on PATH and then blocks on a prompt the first
    time it is asked to do real work. Asking for a version up front turns that
    into a fast, silent unavailability instead of a hung request.
    """
    if shutil.which(command[0]) is None:
        return False
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=20,
            creationflags=subprocess_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def available_tools() -> dict[str, bool]:
    return {
        "pandoc": _probe(["pandoc", "--version"]),
        "tex": _probe([TEX_ENGINE, "--version"]),
    }


def available_formats() -> list[dict[str, object]]:
    """The formats this host can actually produce, in menu order."""
    tools = available_tools()
    return [
        {
            "format": spec.name,
            "label": spec.label,
            "extension": spec.extension,
            "media_type": spec.media_type,
        }
        for spec in FORMATS.values()
        if all(tools.get(requirement, False) for requirement in spec.requires)
    ]


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            timeout=RENDER_TIMEOUT_SECONDS,
            creationflags=subprocess_creation_flags(),
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError(
            "render_timeout",
            f"{command[0]} did not finish within {RENDER_TIMEOUT_SECONDS}s",
            status=504,
        ) from exc
    except OSError as exc:
        raise RenderError(
            "render_tool_unavailable", f"{command[0]} could not be started", status=503
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()[-800:]
        raise RenderError(
            "render_failed", f"{command[0]} failed: {detail}", status=422
        )


def _pandoc_base(fmt: str) -> list[str]:
    command = ["pandoc", "--from", MARKDOWN_READER, "--to", fmt, "--standalone"]
    if _sandbox_supported():
        command.append("--sandbox")
    return command


def _sandbox_supported() -> bool:
    try:
        completed = subprocess.run(
            ["pandoc", "--help"],
            capture_output=True,
            timeout=20,
            creationflags=subprocess_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return b"--sandbox" in completed.stdout


def render(markdown: str, fmt: str) -> bytes:
    """Convert canonical Markdown into the requested deliverable's bytes."""
    spec = FORMATS.get(fmt)
    if spec is None:
        raise RenderError("unsupported_format", f"unknown render format {fmt!r}", status=400)

    source = markdown.encode("utf-8")
    if len(source) > MAX_RENDERED_BYTES:
        raise RenderError(
            "document_too_large",
            "document exceeds the render size limit",
            status=413,
        )

    if spec.name == "markdown":
        return source

    tools = available_tools()
    missing = [name for name in spec.requires if not tools.get(name, False)]
    if missing:
        raise RenderError(
            "render_tool_unavailable",
            f"{spec.label} export needs {', '.join(missing)} on this host",
            status=503,
        )

    with tempfile.TemporaryDirectory(prefix="wb-cowork-render-") as scratch:
        work = Path(scratch)
        (work / "input.md").write_bytes(source)

        if spec.name == "pdf":
            _run(_pandoc_base("latex") + ["-o", "output.tex", "input.md"], cwd=work)
            # openin_any=p confines TeX's file reads to the scratch directory, so
            # an \input that survived as literal text still cannot reach the host.
            env = {
                **_inherited_env(),
                "openin_any": "p",
                "openout_any": "p",
                "TEXMFVAR": str(work / "texmf"),
            }
            _run(
                [
                    "latexmk",
                    f"-{TEX_ENGINE}",
                    "-no-shell-escape",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "output.tex",
                ],
                cwd=work,
                env=env,
            )
            produced = work / "output.pdf"
        else:
            produced = work / f"output{spec.extension}"
            _run(
                _pandoc_base(spec.name) + ["-o", produced.name, "input.md"], cwd=work
            )

        if not produced.is_file():
            raise RenderError(
                "render_failed", f"{spec.label} export produced no file", status=422
            )
        return produced.read_bytes()


def _inherited_env() -> dict[str, str]:
    import os

    return dict(os.environ)
