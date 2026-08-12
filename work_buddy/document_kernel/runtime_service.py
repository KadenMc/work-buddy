"""Process-scoped lifecycle for the packaged document kernel worker."""

from __future__ import annotations

import threading

from work_buddy.document_kernel.client import DocumentKernelClient


_lock = threading.RLock()
_client: DocumentKernelClient | None = None


def shared_document_kernel() -> DocumentKernelClient:
    global _client
    with _lock:
        if _client is None:
            _client = DocumentKernelClient()
        return _client


def reset_document_kernel() -> None:
    """Close the managed worker; the next request starts a clean instance."""

    global _client
    with _lock:
        current = _client
        _client = None
    if current is not None:
        current.close()


__all__ = ["reset_document_kernel", "shared_document_kernel"]
