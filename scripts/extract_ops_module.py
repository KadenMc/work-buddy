"""Extract a registry category builder into an Ops module.

For one ``_<category>_capabilities()`` builder in ``registry.py``, generate
``work_buddy/mcp_server/ops/<category>_ops.py``: the builder's closure/helper
code is moved verbatim into a ``_register()`` function, and the builder's
``return [Capability(...), ...]`` is replaced by ``register_op()`` calls — one
per capability, ``op.wb.<name>`` → the capability's ``callable`` expression.

The builder is NOT removed from ``registry.py`` here — strip it separately
once the generated module imports and resolves cleanly.

    uv run python -m scripts.extract_ops_module <category> <builder_name>
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_REGISTRY = Path("work_buddy/mcp_server/registry.py")
_OPS_DIR = Path("work_buddy/mcp_server/ops")

_HEADER = '''"""{title}-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). The closure code below
is moved verbatim from the former ``registry.py`` builder.
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


'''


def extract(category: str, builder_name: str) -> None:
    src = _REGISTRY.read_text(encoding="utf-8")
    lines = src.split("\n")
    tree = ast.parse(src)

    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == builder_name),
        None,
    )
    if fn is None:
        raise SystemExit(f"builder {builder_name!r} not found")

    # The return statement must be the builder's last statement and a List
    # literal of Capability(...) calls.
    ret = fn.body[-1]
    if not isinstance(ret, ast.Return) or not isinstance(ret.value, ast.List):
        raise SystemExit(
            f"{builder_name}: last statement is not `return [<list>]` — "
            "extract this builder by hand."
        )

    # Names defined inside the builder body (closures, imports, assignments)
    # — anything else a callable references by bare name is a registry.py
    # module-level callable that must be imported into the ops module.
    defined: set[str] = set()
    for stmt in fn.body[:-1]:
        for node in ast.walk(stmt):
            if isinstance(node, ast.FunctionDef):
                defined.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    defined.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        defined.add(t.id)

    regs: list[tuple[str, str]] = []
    from_registry: list[str] = []
    for elt in ret.value.elts:
        if not isinstance(elt, ast.Call):
            raise SystemExit(f"{builder_name}: non-Call element in return list")
        kw = {k.arg: k.value for k in elt.keywords}
        if "name" not in kw or "callable" not in kw:
            raise SystemExit(f"{builder_name}: Capability missing name/callable")
        name = ast.literal_eval(kw["name"])
        cv = kw["callable"]
        callable_expr = ast.unparse(cv)
        if isinstance(cv, ast.Name) and cv.id not in defined:
            if cv.id not in from_registry:
                from_registry.append(cv.id)
        regs.append((name, callable_expr))

    # Builder body source: from the first body statement up to (not incl.)
    # the return statement.
    body_start = fn.body[0].lineno          # 1-based
    body_end = ret.lineno                   # 1-based — return line
    body_lines = lines[body_start - 1:body_end - 1]
    # Drop trailing blank lines before the return.
    while body_lines and body_lines[-1].strip() == "":
        body_lines.pop()

    reg_lines = [
        f'    register_op("op.wb.{name}", {expr})'
        for name, expr in regs
    ]

    title = category.replace("_", " ").title()
    out = [_HEADER.format(title=title)]
    out.append("def _register() -> None:")
    if from_registry:
        # Callables defined at registry.py module scope (not closures).
        names = ", ".join(sorted(from_registry))
        out.append(f"    from work_buddy.mcp_server.registry import {names}")
        out.append("")
    out.extend(body_lines)
    out.append("")
    out.extend(reg_lines)
    out.append("")
    out.append("")
    out.append("_register()")

    target = _OPS_DIR / f"{category}_ops.py"
    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {target} — {len(regs)} ops: {[n for n, _ in regs]}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m scripts.extract_ops_module <category> <builder_name>")
    extract(sys.argv[1], sys.argv[2])
