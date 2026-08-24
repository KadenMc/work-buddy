"""Stable result envelopes for native task-producing integrations."""

from __future__ import annotations

from typing import Any, Mapping

from .store import TaskStore


def native_creation_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Select native task identity/receipt fields and linked document metadata.

    Integration callers intentionally receive no compatibility Markdown line or
    filesystem-path fields after the native authority epoch.  A document link
    is read from the task store so summary-backed creations can immediately
    navigate to their Co-work knowledge document.
    """

    task_id = str(result.get("task_id") or "").strip()
    task = result.get("task")
    task_mapping = task if isinstance(task, Mapping) else {}
    revision = result.get("revision", task_mapping.get("revision"))

    knowledge_document: dict[str, Any] | None = None
    if task_id:
        link = TaskStore().get_task_document_link(task_id)
        if link is not None:
            knowledge_document = {
                **link.to_dict(),
                "href": (
                    f"/app/cowork?store_id={link.store_id}"
                    f"&document_id={link.document_id}"
                ),
            }

    return {
        "task_id": task_id or None,
        "revision": revision,
        "collection_revision": result.get("collection_revision"),
        "receipt": result.get("receipt"),
        "replayed": bool(result.get("replayed", False)),
        "knowledge_document": knowledge_document,
    }


__all__ = ["native_creation_result"]
