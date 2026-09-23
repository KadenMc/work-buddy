"""Only work_buddy/process/ may start operating-system processes.

Every launch outside the package bypasses the policy that keeps background
work windowless, non-interactive, bounded by a timeout, and cleaned up. So
production code under ``work_buddy/`` must not call, or pass as a value:

- ``subprocess.run``, ``Popen``, ``call``, ``check_call``, ``check_output``,
  ``getoutput``, ``getstatusoutput``
- ``os.system``, ``os.popen``, ``os.startfile``, ``os.spawn*``, ``os.exec*``,
  ``os.posix_spawn*``
- ``asyncio.create_subprocess_exec``, ``asyncio.create_subprocess_shell``

nor pass ``creationflags=``, nor name the Windows console-creation flags.

Use the intent that fits instead: ``run_tool``, ``spawn_worker``,
``start_host`` or ``open_visible_terminal`` from ``work_buddy.process``.

Still allowed everywhere: the ``subprocess`` exception types,
``CompletedProcess``, ``DEVNULL``, ``PIPE``, ``STDOUT``, ``list2cmdline``, and
any of these names inside a type annotation.

Launches this rule cannot see, and does not need to: PTY sessions, which go
through winpty or ptyprocess and are windowless by construction, and
``webbrowser.open``, which hands a URL to the desktop's browser.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PACKAGE = REPO / "work_buddy"
BOUNDARY = PACKAGE / "process"

FORBIDDEN: dict[str, frozenset[str]] = {
    "subprocess": frozenset({
        "run", "Popen", "call", "check_call", "check_output",
        "getoutput", "getstatusoutput",
    }),
    "os": frozenset({
        "system", "popen", "startfile",
        "spawnl", "spawnle", "spawnlp", "spawnlpe",
        "spawnv", "spawnve", "spawnvp", "spawnvpe",
        "execl", "execle", "execlp", "execlpe",
        "execv", "execve", "execvp", "execvpe",
        "posix_spawn", "posix_spawnp",
    }),
    "asyncio": frozenset({"create_subprocess_exec", "create_subprocess_shell"}),
}
CONSOLE_FLAGS = frozenset({"CREATE_NO_WINDOW", "CREATE_NEW_CONSOLE", "DETACHED_PROCESS"})

EXEMPT: dict[str, str] = {}
"""Files allowed to launch directly, keyed by repository path, each with the
reason. Adding one needs a reason a reviewer would accept: a launch that the
package cannot express, not one that is merely easier to write directly."""


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    what: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.what}"


def _annotation_nodes(tree: ast.AST) -> set[int]:
    """Every node inside a type annotation, where naming a launch API is fine."""
    inside: set[int] = set()
    for node in ast.walk(tree):
        annotations: list[ast.AST] = []
        if isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                annotations.append(node.returns)
            arguments = node.args
            for arg in (
                *arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                *([arguments.vararg] if arguments.vararg else []),
                *([arguments.kwarg] if arguments.kwarg else []),
            ):
                if arg.annotation is not None:
                    annotations.append(arg.annotation)
        for annotation in annotations:
            inside.update(id(sub) for sub in ast.walk(annotation))
    return inside


def scan_source(source: str, path: str = "<source>") -> list[Violation]:
    """Every launch, launch value, ``creationflags=`` and console flag in
    ``source``, with module aliases and from-imports resolved."""
    tree = ast.parse(source, filename=path)
    module_alias: dict[str, str] = {}
    name_alias: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN:
                    module_alias[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN and node.level == 0:
            for alias in node.names:
                if alias.name in FORBIDDEN[node.module] or alias.name in CONSOLE_FLAGS:
                    name_alias[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    annotations = _annotation_nodes(tree)
    violations: list[Violation] = []

    def report(node: ast.AST, what: str) -> None:
        violations.append(Violation(path, getattr(node, "lineno", 0), what))

    for node in ast.walk(tree):
        if id(node) in annotations:
            continue
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            module = module_alias.get(node.value.id)
            if module and node.attr in FORBIDDEN[module]:
                report(node, f"{module}.{node.attr}")
            elif module == "subprocess" and node.attr in CONSOLE_FLAGS:
                report(node, f"subprocess.{node.attr}")
        elif isinstance(node, ast.Name) and node.id in name_alias:
            report(node, name_alias[node.id])
        elif isinstance(node, ast.Constant) and node.value in CONSOLE_FLAGS:
            report(node, f"the {node.value} flag, by name")
        elif isinstance(node, ast.keyword) and node.arg == "creationflags":
            report(node.value, "creationflags=")
    return sorted(violations, key=lambda v: (v.line, v.what))


def _production_files() -> list[Path]:
    return sorted(
        path for path in PACKAGE.rglob("*.py")
        if BOUNDARY not in path.parents and "__pycache__" not in path.parts
    )


def _relative(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def test_no_production_code_launches_outside_the_process_package() -> None:
    violations = [
        violation
        for path in _production_files()
        if _relative(path) not in EXEMPT
        for violation in scan_source(path.read_text(encoding="utf-8"), _relative(path))
    ]

    assert not violations, (
        "Start processes through work_buddy.process (run_tool, spawn_worker, "
        "start_host or open_visible_terminal), which owns window, console, "
        "stdin, timeout and cleanup policy:\n  "
        + "\n  ".join(str(v) for v in violations)
    )


def test_every_exemption_is_still_needed() -> None:
    stale = [
        path for path in EXEMPT
        if not scan_source((REPO / path).read_text(encoding="utf-8"), path)
    ]

    assert not stale, f"exemptions that no longer launch anything: {stale}"


def test_the_process_package_imports_only_the_standard_library_at_module_level() -> None:
    """Host entry points import the package before their identity exists,
    and hot paths import it on every call, so it must stay light."""
    foreign: list[str] = []
    for path in sorted(BOUNDARY.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] if node.level == 0 else []
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if name.startswith("work_buddy.process") or top == "__future__":
                    continue
                if top not in sys.stdlib_module_names:
                    foreign.append(f"{_relative(path)}: {name}")

    assert not foreign


# -- the scanner itself -------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import subprocess\nsubprocess.run(['git'])", ["subprocess.run"]),
        ("import subprocess as sp\nsp.Popen(['git'])", ["subprocess.Popen"]),
        ("from subprocess import check_output\ncheck_output(['git'])", ["subprocess.check_output"]),
        ("from subprocess import run as go\ngo(['git'])", ["subprocess.run"]),
        ("import os\nos.system('dir')", ["os.system"]),
        ("import os\nos.startfile('a.txt')", ["os.startfile"]),
        ("import os\nos.execv('/bin/sh', ['sh'])", ["os.execv"]),
        (
            "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('git')",
            ["asyncio.create_subprocess_exec"],
        ),
        ("import subprocess\nrunner = subprocess.run", ["subprocess.run"]),
        ("import subprocess\ndef f(run=subprocess.run): ...", ["subprocess.run"]),
        ("import subprocess\nflags = subprocess.CREATE_NO_WINDOW", ["subprocess.CREATE_NO_WINDOW"]),
        ("from subprocess import CREATE_NEW_CONSOLE\nx = CREATE_NEW_CONSOLE", ["subprocess.CREATE_NEW_CONSOLE"]),
        (
            "import subprocess\nx = getattr(subprocess, 'DETACHED_PROCESS', 0)",
            ["the DETACHED_PROCESS flag, by name"],
        ),
        ("def f(start):\n    start(['x'], creationflags=0)", ["creationflags="]),
    ],
)
def test_the_scanner_finds_each_kind_of_launch(source: str, expected: list[str]) -> None:
    found = [violation.what for violation in scan_source(source)]

    for what in expected:
        assert what in found


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess\nsubprocess.TimeoutExpired",
        "import subprocess\nsubprocess.CalledProcessError",
        "import subprocess\nsubprocess.DEVNULL, subprocess.PIPE, subprocess.STDOUT",
        "import subprocess\nsubprocess.list2cmdline(['a b'])",
        "import subprocess\nx: subprocess.CompletedProcess",
        "import subprocess\nprocess: subprocess.Popen | None = None",
        "import subprocess\ndef f(p: subprocess.Popen) -> subprocess.Popen: ...",
        "import os\nos.getpid(), os.kill",
        "from work_buddy.process import run_tool\nrun_tool(['git'], timeout=5)",
        "run = print\nrun('not a launch')",
    ],
)
def test_the_scanner_allows_what_launches_nothing(source: str) -> None:
    assert scan_source(source) == []
