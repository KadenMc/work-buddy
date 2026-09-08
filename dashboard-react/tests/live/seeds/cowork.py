"""Seed an isolated Co-work lifecycle fixture through production domain seams."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

def _required_path(name: str) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return Path(value).resolve()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


root = _required_path("WB_LIVE_ROOT")
host_root = _required_path("WB_LIVE_HOST_ROOT")
data_root = _required_path("WORK_BUDDY_DATA_DIR")
manifest_path = _required_path("WB_LIVE_FIXTURE_FILE")
scenario = os.environ.get("WB_LIVE_SCENARIO", "lifecycle")
if scenario not in {"lifecycle", "truth-panel"}:
    raise RuntimeError(f"unknown Co-work seed scenario: {scenario}")
if not (root / ".wb-live-harness").is_file():
    raise RuntimeError("refusing to seed outside a marked Co-work live temp root")
if any(root not in item.parents for item in (host_root, data_root, manifest_path)):
    raise RuntimeError("fixture paths must remain inside the Co-work live temp root")

ordinary = host_root / "Project Alpha"
initialized = host_root / "Reference Folder"
for folder in (ordinary, initialized):
    folder.mkdir(parents=True, exist_ok=True)


def _write_once(target: Path, contents: bytes) -> None:
    """Keep an existing fixture's edits when the same world is seeded again."""

    if root not in target.resolve().parents:
        raise RuntimeError("fixture file escaped the marked temp root")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as output:
            output.write(contents)
    except FileExistsError:
        pass

root_gitignore = b"# user-owned root ignore\r\n/private-notes/\r\n"
_write_once(ordinary / ".gitignore", root_gitignore)
unrelated_manifest = (
    b"# owned by the fixture's unrelated component\r\n"
    b"format: wbuddy-folder/v1\r\n"
    b"components:\r\n"
    b"  search:\r\n"
    b"    path: search  # preserve this comment\r\n"
)
_write_once(ordinary / ".wbuddy" / "manifest.yaml", unrelated_manifest)
sibling_state = ordinary / ".wbuddy" / "search" / "state.bin"
_write_once(sibling_state, b"unrelated-component-state\x00\x01")

source_relative = "Existing Notes/Imported Note.MD"
source_path = ordinary / Path(source_relative)
source_bytes = b"# Imported note\n\nA line preserved exactly.\n"
_write_once(source_path, source_bytes)

sentinel_path = host_root / "isolation-sentinel.txt"
sentinel_bytes = b"wb-live-isolated\n"
_write_once(sentinel_path, sentinel_bytes)

from work_buddy.cowork.project_store import ProjectStoreManager  # noqa: E402
from work_buddy.truth.registry import TruthStoreRegistry  # noqa: E402

manager = ProjectStoreManager(data_root=data_root)
registry = TruthStoreRegistry()
inspection = manager.inspect(initialized)
while inspection.status == "inspection_pending":
    inspection = manager.inspect(
        initialized, continuation_token=inspection.continuation_token
    )
if inspection.status not in {"uninitialized", "initialized"} or not inspection.fingerprint:
    raise RuntimeError(f"unexpected initialized seed inspection: {inspection.status}")
if inspection.status == "initialized":
    store = manager.open_initialized(
        initialized,
        registry=registry,
        inspection_fingerprint=inspection.fingerprint,
    )
else:
    store = manager.initialize(
        initialized,
        registry=registry,
        inspection_fingerprint=inspection.fingerprint,
        idempotency_key="wb-live-reference-folder-v1",
    )

payload = {
    "format": "wb-live-fixture/v1",
    "root": str(root),
    "host_root": str(host_root),
    "ordinary": {
        "name": ordinary.name,
        "path": str(ordinary),
        "root_gitignore_sha256": _sha256(root_gitignore),
        "unrelated_manifest_sha256": _sha256(unrelated_manifest),
        "unrelated_manifest_base64": base64.b64encode(unrelated_manifest).decode(),
        "sibling_state_sha256": _sha256(sibling_state.read_bytes()),
    },
    "initialized": {
        "name": initialized.name,
        "path": str(initialized),
        "store_id": store.store_id,
    },
    "source": {
        "relative_path": source_relative,
        "path": str(source_path),
        "sha256": _sha256(source_bytes),
        "byte_length": len(source_bytes),
        "base64": base64.b64encode(source_bytes).decode(),
    },
    "sentinel": {
        "path": str(sentinel_path),
        "sha256": _sha256(sentinel_bytes),
    },
}
if manifest_path.exists():
    existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    if existing.get("root") != str(root) or existing.get("format") != payload["format"]:
        raise RuntimeError("fixture manifest does not describe this marked root")
    payload = {**existing, **payload}
if scenario == "truth-panel":
    from truth_panel import seed_truth_panel

    payload["truth_panel"] = seed_truth_panel(
        root=root,
        folder=initialized,
        store=registry.open_store(store.store_id),
        existing=payload.get("truth_panel"),
    )
manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
print(json.dumps({"ok": True, "fixture_format": payload["format"]}))
