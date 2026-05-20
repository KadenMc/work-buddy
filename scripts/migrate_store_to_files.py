"""One-time conversion: multi-unit JSON store -> file-per-unit Markdown.

Rewrites the system knowledge store from ``knowledge/store/*.json`` (multi-unit
JSON files) to one Markdown file per unit (``knowledge/store/<path>.md``).

The conversion is **non-destructive until it has verified itself**:

  1. Load the current JSON store.
  2. Write every unit to its ``.md`` file via the file-store codec.
  3. Load the store back from the ``.md`` files and assert it is unit-for-unit
     identical to the JSON load (same paths, same field values, same content).
  4. Only if the assert passes, delete the ``*.json`` store files.

A failed assert costs nothing — the JSON files are still there and the written
``.md`` files can be discarded with ``git clean``.

Scope safety — the one irreversible failure mode this migration must avoid:
the conversion reads, writes, and deletes **strictly within
``knowledge/store/``**. It never touches the Obsidian vault or
``knowledge/store.local/`` — both are gitignored and not git-recoverable.
Every target path is asserted to resolve under ``knowledge/store/`` before any
write or delete.

Run from the work-buddy conda env:

    conda run -n work-buddy python -m scripts.migrate_store_to_files [--dry-run]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_STORE_DIR = _REPO_ROOT / "knowledge" / "store"


def _assert_under_store(path: Path) -> None:
    """Refuse any path that does not resolve strictly under knowledge/store/."""
    resolved = path.resolve()
    store = _STORE_DIR.resolve()
    if resolved != store and store not in resolved.parents:
        raise RuntimeError(
            f"SCOPE VIOLATION: {resolved} is not under {store}. Refusing to "
            "write or delete outside the system knowledge store."
        )


def _normalize(unit) -> dict[str, Any]:
    """Comparable view of a typed unit.

    ``children`` is excluded — it is a derived field, stored-and-reconciled in
    the JSON substrate and derived-from-parents in the file substrate; ``parents``
    (compared strictly) is the authoritative edge. ``content.full`` is rstripped:
    a handful of units carry an inconsistent trailing newline in their JSON
    string, which the file substrate canonicalizes away with no semantic change.
    """
    d = unit.to_dict()
    d.pop("children", None)
    content = d.get("content")
    if isinstance(content, dict) and isinstance(content.get("full"), str):
        content = dict(content)
        content["full"] = content["full"].rstrip("\n")
        d["content"] = content
    return d


def convert_store(*, apply_delete: bool = True) -> dict[str, Any]:
    """Run the JSON -> file-per-unit conversion. Returns a report dict."""
    from work_buddy.knowledge.file_store import write_unit
    from work_buddy.knowledge.store import load_store

    _assert_under_store(_STORE_DIR)

    existing_md = list(_STORE_DIR.rglob("*.md"))
    if existing_md:
        raise RuntimeError(
            f"{len(existing_md)} .md file(s) already exist under {_STORE_DIR}. "
            "The store appears already (partially) converted. Discard them with "
            "'git clean -fd knowledge/store/' and re-run, or skip the conversion."
        )

    json_files = sorted(_STORE_DIR.glob("*.json"))
    if not json_files:
        raise RuntimeError(f"No *.json store files found under {_STORE_DIR}.")

    # 1. Load the JSON store and snapshot it (the cache is reused on reload).
    store_json = load_store(force=True)
    snapshot = {path: _normalize(unit) for path, unit in store_json.items()}
    print(f"Loaded {len(snapshot)} units from {len(json_files)} JSON files.")

    # 2. Write every unit to its .md file.
    for path, unit in sorted(store_json.items()):
        target = _STORE_DIR / f"{path}.md"
        _assert_under_store(target)
        write_unit(_STORE_DIR, path, unit.to_dict())
    written = list(_STORE_DIR.rglob("*.md"))
    print(f"Wrote {len(written)} .md unit files.")

    # 3. Reload from the .md files and assert unit-for-unit identity.
    store_md = load_store(force=True)
    reloaded = {path: _normalize(unit) for path, unit in store_md.items()}

    mismatches: list[str] = []
    only_json = sorted(set(snapshot) - set(reloaded))
    only_md = sorted(set(reloaded) - set(snapshot))
    for path in only_json:
        mismatches.append(f"  missing after conversion: {path}")
    for path in only_md:
        mismatches.append(f"  appeared after conversion: {path}")
    for path in sorted(set(snapshot) & set(reloaded)):
        if snapshot[path] != reloaded[path]:
            a, b = snapshot[path], reloaded[path]
            fields = sorted(
                k for k in set(a) | set(b) if a.get(k) != b.get(k)
            )
            mismatches.append(f"  field mismatch at {path}: {fields}")

    if mismatches:
        print(f"\nCONVERSION FAILED — {len(mismatches)} mismatch(es):")
        for m in mismatches[:50]:
            print(m)
        print(
            "\nThe JSON store is intact. Discard the written .md files with "
            "'git clean -fd knowledge/store/'."
        )
        return {
            "status": "failed",
            "units": len(snapshot),
            "mismatches": len(mismatches),
        }

    print(f"Verified: {len(reloaded)} units identical across both substrates.")

    # 4. Destructive step — gated on the passing assert.
    if not apply_delete:
        print("--dry-run: leaving JSON files in place.")
        return {
            "status": "verified",
            "units": len(reloaded),
            "json_files": len(json_files),
        }

    for json_file in json_files:
        _assert_under_store(json_file)
        json.loads(json_file.read_text(encoding="utf-8"))  # last sanity read
        json_file.unlink()
    print(f"Deleted {len(json_files)} JSON store files.")

    return {
        "status": "converted",
        "units": len(reloaded),
        "md_files": len(written),
        "json_files_deleted": len(json_files),
    }


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    result = convert_store(apply_delete=not dry_run)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["status"] in ("converted", "verified") else 1)
