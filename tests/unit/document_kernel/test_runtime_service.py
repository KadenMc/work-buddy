from __future__ import annotations

import pytest

from work_buddy.document_kernel import runtime_service
from work_buddy.document_kernel.direct_edit import DirectDocumentEditService
from work_buddy.document_kernel.domain_service import RunningNoteDocumentService
from work_buddy.tasks.documents import TaskDocumentService


class _FakeKernel:
    instances: list["_FakeKernel"] = []

    def __init__(self) -> None:
        self.close_calls = 0
        self.instances.append(self)

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture(autouse=True)
def _isolated_runtime(monkeypatch: pytest.MonkeyPatch):
    runtime_service.reset_document_kernel()
    _FakeKernel.instances.clear()
    monkeypatch.setattr(runtime_service, "DocumentKernelClient", _FakeKernel)
    yield
    runtime_service.reset_document_kernel()


def test_default_document_services_share_one_process_scoped_kernel() -> None:
    task_services = [TaskDocumentService() for _ in range(20)]
    direct_services = [DirectDocumentEditService() for _ in range(20)]
    note_services = [RunningNoteDocumentService() for _ in range(20)]

    kernels = {
        id(service.kernel)
        for service in (*task_services, *direct_services, *note_services)
    }
    assert len(kernels) == 1
    assert len(_FakeKernel.instances) == 1


def test_reset_closes_shared_kernel_and_next_request_gets_a_fresh_one() -> None:
    first = runtime_service.shared_document_kernel()

    runtime_service.reset_document_kernel()

    assert first.close_calls == 1
    second = runtime_service.shared_document_kernel()
    assert second is not first
    assert len(_FakeKernel.instances) == 2
