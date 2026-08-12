"""Trusted headless structured-document kernel and document causality services."""

from work_buddy.document_kernel.client import (
    DocumentKernelClient,
    DocumentKernelError,
    KernelAmbiguousCompletion,
    KernelOutcome,
    KernelProtocolError,
    KernelUnavailable,
)
from work_buddy.document_kernel.direct_edit import DirectDocumentEditService, DirectEditResult
from work_buddy.document_kernel.pilot import RunningNotePilotResult, RunningNotePilotService

__all__ = [
    "DocumentKernelClient",
    "DirectDocumentEditService",
    "DirectEditResult",
    "DocumentKernelError",
    "KernelAmbiguousCompletion",
    "KernelOutcome",
    "KernelProtocolError",
    "KernelUnavailable",
    "RunningNotePilotResult",
    "RunningNotePilotService",
]
