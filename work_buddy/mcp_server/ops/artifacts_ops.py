"""Artifacts-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). The closure code below
is moved verbatim from the former ``registry.py`` builder.
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op



def _register() -> None:
    def artifact_save(
        content: str,
        type: str,
        slug: str,
        ext: str = "json",
        tags: str = "",
        description: str = "",
        ttl_days: int | None = None,
        agent_session_id: str = "",
    ) -> dict:
        from work_buddy.artifacts import get_store

        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        sid = agent_session_id or None

        store = get_store()
        rec = store.save(
            content=content,
            type=type,
            slug=slug,
            ext=ext,
            tags=tag_list,
            description=description,
            session_id=sid,
            ttl_days=ttl_days,
        )
        return rec.to_dict()

    def artifact_list(
        type: str = "",
        since: str = "",
        tags: str = "",
        session_id: str = "",
        include_expired: bool = False,
        limit: int = 50,
    ) -> dict:
        from work_buddy.artifacts import get_store
        from datetime import datetime

        since_dt = datetime.fromisoformat(since) if since else None
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None

        store = get_store()
        records = store.list(
            type=type or None,
            since=since_dt,
            tags=tag_list,
            session_id=session_id or None,
            include_expired=include_expired,
            limit=limit,
        )
        return {"count": len(records), "artifacts": [r.to_dict() for r in records]}

    def artifact_get(id: str) -> dict:
        from work_buddy.artifacts import get_store

        store = get_store()
        rec = store.get(id)
        result = rec.to_dict()
        # Include content inline if small enough (< 50KB)
        if rec.size_bytes < 50_000:
            try:
                result["content"] = rec.path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                result["content"] = "(binary content — use file path to read)"
        else:
            result["content"] = f"(large file: {rec.size_bytes} bytes — use file path to read)"
        return result

    def artifact_delete(id: str) -> dict:
        from work_buddy.artifacts import get_store

        store = get_store()
        found = store.delete(id)
        return {"deleted": found, "id": id}

    def artifact_cleanup(dry_run: bool = True, name: str = "") -> dict:
        """Run TTL-based cleanup over registered artifacts.

        Drives off the unified artifact registry (every consumer that
        registered an Artifact participates). Pass ``name`` to scope
        the sweep to a single artifact (e.g. ``"llm-cache"``,
        ``"messages"``). Returns the legacy result dict shape for
        backward compat with existing callers.
        """
        from work_buddy.artifacts import get_store
        from work_buddy.artifacts.registry import sweep_all

        if name:
            # Scoped sweep: just one artifact, return its SweepResult
            # in the per-pruner shape so callers can introspect it.
            results = sweep_all(dry_run=dry_run, name=name)
            sr = results[0] if results else None
            if sr is None:
                return {"dry_run": dry_run, "name": name, "error": "unknown artifact"}
            return {
                "dry_run": dry_run,
                "name": sr.artifact_name,
                "pruned": sr.pruned,
                "remaining": sr.remaining,
                "bytes_before": sr.bytes_before,
                "bytes_after": sr.bytes_after,
                **({"transformed": sr.transformed} if sr.transformed else {}),
                **({"error": sr.error} if sr.error else {}),
                **sr.extra,
            }

        # Default path — run the full cleanup orchestrator on the
        # filesystem store, which now drives off the registry.
        store = get_store()
        return store.cleanup(dry_run=dry_run)

    def artifact_registry() -> dict:
        """Return the cross-backend artifact-registry introspection map.

        For each registered artifact, returns its name, storage kind,
        lifecycle kind, provenance kind, declared capabilities, and
        the operations it exposes via MCP. This is the single place
        agents and operators look to see "what does each persisted
        resource in work-buddy look like."
        """
        from work_buddy.artifacts.registry import artifact_registry_dump

        registry = artifact_registry_dump()
        return {
            "count": len(registry),
            "artifacts": registry,
        }

    def commit_record(
        commit_hash: str,
        message: str,
        branch: str = "",
        files_changed: str = "",
        tests_run: str = "",
        tests_passed: int = 0,
        tests_failed: int = 0,
        knowledge_units_updated: str = "",
        summary: str = "",
        agent_session_id: str = "",
    ) -> dict:
        """Record structured commit metadata as an artifact."""
        import json
        from work_buddy.artifacts import get_store

        files_list = [f.strip() for f in files_changed.split(",") if f.strip()] if files_changed else []
        tests_list = [t.strip() for t in tests_run.split(",") if t.strip()] if tests_run else []
        ku_list = [k.strip() for k in knowledge_units_updated.split(",") if k.strip()] if knowledge_units_updated else []

        record = {
            "commit_hash": commit_hash,
            "message": message,
            "branch": branch,
            "files_changed": files_list,
            "tests": {
                "files_run": tests_list,
                "passed": tests_passed,
                "failed": tests_failed,
            },
            "knowledge_units_updated": ku_list,
            "summary": summary,
        }

        store = get_store()
        slug = f"commit-{commit_hash[:7]}"
        rec = store.save(
            content=json.dumps(record, indent=2),
            type="commit",
            slug=slug,
            ext="json",
            tags=["commit", branch] if branch else ["commit"],
            description=summary or message[:80],
            session_id=agent_session_id or None,
        )

        result = rec.to_dict()
        result["record"] = record
        return result

    register_op("op.wb.artifact_save", artifact_save)
    register_op("op.wb.artifact_list", artifact_list)
    register_op("op.wb.artifact_get", artifact_get)
    register_op("op.wb.artifact_delete", artifact_delete)
    register_op("op.wb.artifact_cleanup", artifact_cleanup)
    register_op("op.wb.artifact_registry", artifact_registry)
    register_op("op.wb.commit_record", commit_record)


_register()
