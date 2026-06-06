"""Vault-index domain ops: ``vault_search`` (hybrid search) + ``vault_index`` (build/status).

Both are thin gateway-side dispatchers. Search and build POST to the embedding
service so search serves from the **resident** vector matrix and the build encodes
**in-service** (shared ``LocalInferenceBroker``, BACKGROUND priority). Each degrades
sanely when the service is unreachable. ``status`` is computed locally (pure SQLite),
so "what's indexed" answers even with the embedding service down.

Lazy imports inside the callables keep backends off the gateway boot path.
"""
from __future__ import annotations

import json
import logging

from work_buddy.mcp_server.op_registry import register_op

logger = logging.getLogger(__name__)


def _vault_search_dispatch(
    query: str,
    *,
    top_k: int = 10,
    method: str = "hybrid",
    vault_id: str | None = None,
    recency: bool = False,
) -> str:
    """Hybrid (lexical ⊕ dense) semantic search over the vault index → markdown.

    POSTs to the embedding service's ``/vault/search`` (warm resident matrix). If the
    service is unreachable, degrades to an in-process **lexical-only** search (FTS5,
    no vector-matrix load) so the user still gets results instead of an error.
    """
    from work_buddy.mcp_server.result_format import format_results

    query = (query or "").strip()
    if not query:
        return "No query provided."

    from work_buddy.embedding.client import vault_search as _vault_search_client

    results = _vault_search_client(
        query, top_k=top_k, method=method, vault_id=vault_id, recency=recency,
    )
    label = f"vault:{method}"
    if results is None:
        # Service down → in-process lexical-only fallback (cheap; no matrix load).
        from work_buddy.vault_index.search import search as _local_search

        try:
            results = _local_search(
                query, top_k=top_k, method="lexical", vault_id=vault_id,
            )
        except Exception as exc:
            return json.dumps({"error": f"vault search unavailable: {exc}"})
        label = "vault:lexical (embedding service down — degraded)"
    return format_results(results, label)


def _vault_index_dispatch(action: str = "build", force: bool = False) -> str:
    """Build or check the vault semantic index.

    - ``status``: counts read **locally** (no service dependency).
    - ``build``: skip when a build already holds the advisory lock (read-only
      ``is_locked`` probe — the cron and a manual run can overlap), else POST to the
      embedding service's ``/vault/index`` so the encode runs in-service.
    """
    if action == "status":
        from work_buddy.vault_index.status import index_status

        return json.dumps(index_status(), indent=2)

    # build — skip a tick already in progress (per-index advisory lock).
    from work_buddy.utils import index_lock
    from work_buddy.vault_index import store

    if index_lock.is_locked(store._db_path()):
        return json.dumps({"skipped": True, "reason": "build_in_progress"})

    from work_buddy.embedding.client import vault_index as _vault_index_client
    from work_buddy.utils.service_hints import sidecar_restart_command

    result = _vault_index_client(action="build", force=force)
    if result is None:
        # Genuine connection failure — the service is sidecar-supervised.
        return json.dumps({
            "error": (
                "Embedding service unreachable. It's supervised by the sidecar — "
                f"restart with: {sidecar_restart_command()}. Or run /wb-setup-help "
                "to diagnose."
            )
        })
    if "error" in result:
        # Reached but failed — surface the real error, don't mask it.
        status = result.get("status")
        detail = f" (HTTP {status})" if status else ""
        return json.dumps({"error": f"/vault/index failed{detail}: {result['error']}"})
    return json.dumps(result, indent=2)


def _vault_has_chunks(vault_id: str) -> bool:
    """Whether a vault still has indexed chunks (→ removal would orphan them)."""
    try:
        from work_buddy.vault_index import store
        conn = store.get_connection()
        try:
            return store.chunk_count(conn, vault_id) > 0
        finally:
            conn.close()
    except Exception:
        return False


def _first_bad_glob(patterns: list[str]) -> str | None:
    """Return the first un-parseable gitignore glob, or None if all parse."""
    try:
        from pathspec import PathSpec
    except Exception:
        return None  # pathspec absent → skip validation rather than block
    for g in patterns:
        try:
            PathSpec.from_lines("gitignore", [g])
        except Exception:
            return g
    return None


def _vault_config_dispatch(
    action: str = "set",
    id: str = "",
    path: str = "",
    include: list | None = None,
    exclude: list | None = None,
) -> dict:
    """Add/update (``set``) or remove (``remove``) a vault in ``vault_index.vaults``.

    Persists to ``config.local.yaml`` (the user-override layer; comments there are
    not preserved — it's machine-managed). Changes apply on the **next vault build**
    (the 5-min cron, or a manual ``vault_index`` build) — no restart. ``remove``
    deletes only the config entry; the vault's already-indexed chunks remain
    searchable until an explicit prune.

    Returns ``{"success": bool, ...}``; on validation failure includes
    ``errors_by_field`` so the dashboard form can highlight the offending inputs.
    """
    from work_buddy.config import read_config_local, write_config_local

    vid = (id or "").strip()
    action = (action or "set").strip().lower()
    if action not in ("set", "remove"):
        return {"success": False, "error": f"unknown action {action!r} (expected set|remove)"}

    errors: dict[str, str] = {}
    if not vid:
        errors["id"] = "Vault id is required."
    elif "/" in vid or "\\" in vid:
        errors["id"] = "Vault id must not contain '/' or '\\' (it namespaces chunk ids)."

    local = read_config_local()
    vi = dict(local.get("vault_index") or {})
    vaults = dict(vi.get("vaults") or {})

    if action == "remove":
        if errors:
            return {"success": False, "errors_by_field": errors}
        was_configured = vid in vaults
        vaults.pop(vid, None)
        vi["vaults"] = vaults
        write_config_local("vault_index", vi)
        orphaned = _vault_has_chunks(vid)
        return {
            "success": True, "action": "remove", "id": vid,
            "was_configured": was_configured,
            "orphaned_chunks": orphaned,
            "note": (
                "Config entry removed. Its indexed chunks remain searchable until an "
                "explicit prune." if orphaned else "Removed (no indexed chunks remained)."
            ),
        }

    # ---- set: validate path + globs ----
    p = (path or "").strip()
    if not p:
        errors["path"] = "Path is required."
    inc = [g.strip() for g in (include or []) if g and g.strip()] or ["**/*.md"]
    exc = [g.strip() for g in (exclude or []) if g and g.strip()]
    bad = _first_bad_glob(inc + exc)
    if bad is not None:
        errors["globs"] = f"Invalid glob pattern: {bad!r}"
    if errors:
        return {"success": False, "errors_by_field": errors}

    warning = None
    try:
        from pathlib import Path
        rp = Path(p)
        if not rp.is_absolute():
            from work_buddy.paths import repo_root
            rp = repo_root() / p
        if not rp.is_dir():
            warning = (
                f"Path is not currently a readable directory: {p}. Saved anyway — the "
                "index keeps existing chunks and will pick it up once reachable."
            )
    except Exception:
        pass

    # Preserve the implicit default vault when leaving default mode. With no
    # explicit vaults, the indexer synthesizes a default vault (the user's main
    # root). Adding the FIRST explicit vault would otherwise drop that default —
    # silently orphaning the main vault. Snapshot the current effective vaults
    # into the explicit config first, then apply this edit on top (so promoting
    # the default — id "vault" — just overrides its snapshotted entry).
    promoted_default = False
    if not vaults:
        try:
            from work_buddy.vault_index.source import load_vault_configs
            for vc in load_vault_configs():
                vaults[vc.id] = {
                    "path": str(vc.root),
                    "include": list(vc.include),
                    "exclude": list(vc.exclude),
                }
            promoted_default = vid not in vaults  # a brand-new id alongside the default
        except Exception:
            pass

    vaults[vid] = {"path": p, "include": inc, "exclude": exc}
    vi["vaults"] = vaults
    write_config_local("vault_index", vi)
    note = "Saved. The index reconciles on the next build (≤5 min) — or click Rebuild now."
    if promoted_default:
        note = ("Saved. Your existing default vault was made explicit too (so it keeps "
                "indexing). " + note)
    return {
        "success": True, "action": "set", "id": vid, "warning": warning,
        "promoted_default": promoted_default, "note": note,
    }


def _register() -> None:
    # replace=True so a registry reload (pytest collection / mcp reload re-import)
    # re-binds cleanly rather than crashing on the already-registered name.
    register_op("op.wb.vault_search", _vault_search_dispatch, replace=True)
    register_op("op.wb.vault_index", _vault_index_dispatch, replace=True)
    register_op("op.wb.vault_config", _vault_config_dispatch, replace=True)


_register()
