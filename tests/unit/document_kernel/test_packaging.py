from __future__ import annotations

import tomllib
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
RUNTIME_RELATIVE = "work_buddy/document_kernel/runtime_dist/worker.mjs"


def test_production_runtime_has_one_explicitly_packaged_artifact() -> None:
    runtime = REPO / "work_buddy" / "document_kernel" / "runtime_dist"
    assert sorted(path.name for path in runtime.iterdir()) == ["worker.mjs"]
    assert (runtime / "worker.mjs").stat().st_size > 0

    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    targets = config["tool"]["hatch"]["build"]["targets"]
    for target in ("wheel", "sdist"):
        assert targets[target]["force-include"][RUNTIME_RELATIVE] == RUNTIME_RELATIVE


def test_kernel_build_excludes_dashboard_public_assets() -> None:
    config = (REPO / "dashboard-react" / "vite.document-kernel.config.ts").read_text(
        encoding="utf-8"
    )
    assert "publicDir: false" in config
    verifier = (
        REPO / "dashboard-react" / "scripts" / "verify-document-kernel-build.mjs"
    ).read_text(encoding="utf-8")
    assert 'entries[0] !== "worker.mjs"' in verifier
