from __future__ import annotations

import ast
from pathlib import Path


_LEGACY_IMPORT_BOUNDARY = {"migration.py", "import_legacy.py"}


def test_neutral_task_domain_has_no_vault_or_plugin_dependency():
    root = Path(__file__).parents[3] / "work_buddy" / "tasks"
    joined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in root.glob("*.py")
        if path.name not in _LEGACY_IMPORT_BOUNDARY
    ).casefold()
    forbidden = (
        "work_buddy.obsidian",
        "tasks plugin",
        "datacore",
        "master-task-list.md",
        "tasks/archive.md",
    )
    assert not [token for token in forbidden if token in joined]


def test_runtime_task_modules_cannot_reach_the_one_way_legacy_importer():
    package_root = Path(__file__).parents[3] / "work_buddy"
    task_root = package_root / "tasks"
    absolute_forbidden = {
        "work_buddy.tasks.migration",
        "work_buddy.tasks.import_legacy",
    }
    findings: list[str] = []
    for path in package_root.rglob("*.py"):
        if path.parent == task_root and path.name in _LEGACY_IMPORT_BOUNDARY:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names} & absolute_forbidden
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = (
                    {module} & absolute_forbidden
                    if node.level == 0
                    else (
                        {f".{module}"}
                        if path.parent == task_root
                        and node.level == 1
                        and module in {"migration", "import_legacy"}
                        else set()
                    )
                )
            else:
                continue
            for name in sorted(names):
                findings.append(f"{path.relative_to(package_root)}: {name}")
    assert not findings


def test_legacy_import_operator_cannot_move_delete_or_acl_the_source_tree():
    root = Path(__file__).parents[3] / "work_buddy" / "tasks"
    joined = "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in sorted(_LEGACY_IMPORT_BOUNDARY)
    ).casefold()
    destructive_tokens = (
        ".unlink(",
        ".rename(",
        "shutil.move",
        "shutil.rmtree",
        "remove-item",
        "icacls",
    )
    assert not [token for token in destructive_tokens if token in joined]
