"""Seed an isolated Co-work lifecycle fixture through production domain seams."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from work_buddy.cowork.project_store import ProjectStoreManager
from work_buddy.truth.registry import TruthStoreRegistry


def _required_path(name: str) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return Path(value).resolve()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


root = _required_path("COWORK_LIVE_ROOT")
host_root = _required_path("COWORK_LIVE_HOST_ROOT")
data_root = _required_path("WORK_BUDDY_DATA_DIR")
manifest_path = _required_path("COWORK_LIVE_FIXTURE_FILE")
if not (root / ".cowork-live-harness").is_file():
    raise RuntimeError("refusing to seed outside a marked Co-work live temp root")
if root not in host_root.parents or root not in data_root.parents:
    raise RuntimeError("fixture paths must remain inside the Co-work live temp root")

ordinary = host_root / "Project Alpha"
initialized = host_root / "Reference Folder"
for folder in (ordinary, initialized):
    folder.mkdir(parents=True, exist_ok=False)

root_gitignore = b"# user-owned root ignore\r\n/private-notes/\r\n"
(ordinary / ".gitignore").write_bytes(root_gitignore)
unrelated_manifest = (
    b"# owned by the fixture's unrelated component\r\n"
    b"format: wbuddy-folder/v1\r\n"
    b"components:\r\n"
    b"  search:\r\n"
    b"    path: search  # preserve this comment\r\n"
)
(ordinary / ".wbuddy").mkdir()
(ordinary / ".wbuddy" / "manifest.yaml").write_bytes(unrelated_manifest)
sibling_state = ordinary / ".wbuddy" / "search" / "state.bin"
sibling_state.parent.mkdir()
sibling_state.write_bytes(b"unrelated-component-state\x00\x01")

source_relative = "Existing Notes/Imported Note.MD"
source_path = ordinary / Path(source_relative)
source_path.parent.mkdir()
source_bytes = b"# Imported note\n\nA line preserved exactly.\n"
source_path.write_bytes(source_bytes)

sentinel_path = host_root / "isolation-sentinel.txt"
sentinel_bytes = b"cowork-live-isolated\n"
sentinel_path.write_bytes(sentinel_bytes)

manager = ProjectStoreManager(data_root=data_root)
registry = TruthStoreRegistry()
inspection = manager.inspect(initialized)
while inspection.status == "inspection_pending":
    inspection = manager.inspect(
        initialized, continuation_token=inspection.continuation_token
    )
if inspection.status != "uninitialized" or not inspection.fingerprint:
    raise RuntimeError(f"unexpected initialized seed inspection: {inspection.status}")
store = manager.initialize(
    initialized,
    registry=registry,
    inspection_fingerprint=inspection.fingerprint,
    idempotency_key="cowork-live-reference-folder-v1",
)

payload = {
    "format": "cowork-live-fixture/v1",
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
manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
print(json.dumps({"ok": True, "fixture_format": payload["format"]}))
