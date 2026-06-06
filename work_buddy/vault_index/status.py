"""Status surface for the vault semantic index.

Composes the store's count primitives into a status dict shaped like the IR
engine's ``ir.store.index_status`` — the single source of truth consumed by the
``vault_index`` capability's ``status`` action, the ``/vault/index`` status path,
and the indexing-seam vault adapter. Pure SQLite reads (no numpy, no embedding
service), so status answers even when the embedding service is down.
"""
from __future__ import annotations

from typing import Any

from work_buddy.vault_index import store


def _configured_vaults(cfg: dict) -> dict[str, dict]:
    """``{vault_id: spec}`` from the explicit ``vault_index.vaults`` mapping.

    ``vault_index.vaults`` is a DICT keyed by id (``{vault: {path, include,
    exclude}}``) — see ``source.load_vault_configs``. Empty (``{}`` / absent)
    means "no explicit vaults"; the indexer then walks a single default root
    under id ``"vault"``. In that mode there is no config to be absent from, so
    ``index_status`` must NOT flag the default vault as an orphan.
    """
    vaults = (cfg.get("vault_index", {}) or {}).get("vaults") or {}
    if not isinstance(vaults, dict):  # list/None/garbage → treat as default mode
        return {}
    return {str(vid): (spec or {}) for vid, spec in vaults.items()}


def index_status(cfg: dict | None = None) -> dict[str, Any]:
    """Per-vault + whole-store counts for the vault index.

    Returns ``{"status": "no_index"}`` before the first build; otherwise totals,
    DB size, last build, and a per-vault breakdown (chunks / vectors / pending /
    files / health). ``health`` is ``"unreachable"`` when the indexer's
    debounced ``unreachable_warned:<id>`` flag is set (no filesystem walk here).
    """
    if cfg is None:
        from work_buddy.config import load_config
        cfg = load_config()

    db = store._db_path(cfg)
    if not db.exists():
        return {"status": "no_index", "db_path": str(db)}

    configured = _configured_vaults(cfg)
    conn = store.get_connection(cfg)
    try:
        # Per-vault aggregates via two index-only GROUP BYs (covered by
        # idx_chunks_vault_doc) — no scan of the big text columns. Pending is
        # derived as chunks − vectors rather than a LEFT-JOIN/TRIM scan, which
        # is both faster and a clearer "chunks not yet vectorized" definition.
        chunks_by_vault = {
            r["vid"]: r["n"]
            for r in conn.execute(
                "SELECT vault_id AS vid, COUNT(*) AS n FROM chunks GROUP BY vault_id"
            )
        }
        vectors_by_vault = {
            r["vid"]: r["n"]
            for r in conn.execute(
                "SELECT c.vault_id AS vid, COUNT(*) AS n "
                "FROM chunks c JOIN chunk_vectors v ON c.doc_id = v.doc_id "
                "GROUP BY c.vault_id"
            )
        }
        files_by_vault = {
            r["vid"]: r["n"]
            for r in conn.execute(
                "SELECT vault_id AS vid, COUNT(*) AS n FROM indexed_items GROUP BY vault_id"
            )
        }
        total_chunks = sum(chunks_by_vault.values())
        total_vectors = sum(vectors_by_vault.values())
        last_build = store.get_meta(conn, "last_build:vault_index")
        build_version = store.get_meta(conn, "build_version:vault")

        vaults_info: dict[str, dict[str, Any]] = {}
        for vid in sorted(set(chunks_by_vault) | set(configured)):
            cc = chunks_by_vault.get(vid, 0)
            vc = vectors_by_vault.get(vid, 0)
            warned = store.get_meta(conn, f"unreachable_warned:{vid}")
            vaults_info[vid] = {
                "chunk_count": cc,
                "vector_count": vc,
                "pending": max(0, cc - vc),
                "file_count": files_by_vault.get(vid, 0),
                # "not in config" only means something when an explicit vaults
                # list exists; in default-vault mode every vault is accounted for.
                "in_config": (not configured) or (vid in configured),
                "health": "unreachable" if warned else "ok",
            }
    finally:
        conn.close()

    return {
        "status": "ok",
        "db_path": str(db),
        "size_on_disk_mb": round(db.stat().st_size / 1024 / 1024, 1),
        "total_chunks": total_chunks,
        "total_vectors": total_vectors,
        "pending": max(0, total_chunks - total_vectors),
        "last_build": last_build,
        "build_version": build_version,
        "vaults": vaults_info,
    }


def effective_vault_configs(cfg: dict | None = None) -> list[dict[str, Any]]:
    """The User-table + editor view: each vault's *effective* config + its counts.

    Unions the configured/effective vaults (``source.load_vault_configs`` — which
    synthesizes the default vault when none are explicit) with the vaults that have
    chunks in the store (``index_status``). A vault present in the store but absent
    from config (removed but not yet pruned) surfaces as an orphan
    (``in_config=False``). The single source of truth for the dashboard vault rows
    and the inline config editor, so the two never drift.
    """
    if cfg is None:
        from work_buddy.config import load_config
        cfg = load_config()

    from work_buddy.vault_index.source import load_vault_configs

    explicit = _configured_vaults(cfg)  # {} in default-vault mode
    is_default_mode = not explicit

    effective: dict[str, dict] = {}
    try:
        for vc in load_vault_configs(cfg):
            effective[vc.id] = {
                "path": str(vc.root),
                "include": list(vc.include),
                "exclude": list(vc.exclude),
                "is_default": is_default_mode and vc.id == "vault",
            }
    except Exception:  # a bad vault_root must not blank the table
        pass

    status = index_status(cfg)
    vaults_status = status.get("vaults", {}) if status.get("status") == "ok" else {}

    rows: list[dict[str, Any]] = []
    for vid in sorted(set(effective) | set(vaults_status)):
        eff = effective.get(vid)
        st = vaults_status.get(vid, {})
        rows.append({
            "id": vid,
            "path": (eff or {}).get("path", ""),
            "include": (eff or {}).get("include", []),
            "exclude": (eff or {}).get("exclude", []),
            "is_default": bool((eff or {}).get("is_default", False)),
            # In config when it has an explicit entry, or it's the synthesized
            # default; an in-store-but-not-effective vault is an orphan.
            "in_config": (vid in explicit) or (is_default_mode and eff is not None),
            "chunk_count": st.get("chunk_count", 0),
            "vector_count": st.get("vector_count", 0),
            "pending": st.get("pending", 0),
            "file_count": st.get("file_count", 0),
            "health": st.get("health", "ok"),
            "last_build": status.get("last_build"),
        })
    return rows
