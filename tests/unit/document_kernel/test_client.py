from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.document_kernel.protocol import (
    KernelAmbiguousCompletion,
    KernelProtocolError,
    sha256_bytes,
    structured_head_sha256,
)


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")


def test_packaged_worker_is_dom_free_and_source_round_trips() -> None:
    source = "# Café 🧭\n\nA **marked** paragraph.\n".encode()
    with DocumentKernelClient() as client:
        assert client.health() == {"status": "ready", "domPresent": False}
        bootstrapped = client.request(
            {
                "kind": "bootstrap_markdown",
                "sourceBase64": source,
                "sourceSha256": sha256_bytes(source),
                "newlineStyle": "lf",
                "utf8Bom": False,
                "trailingNewlineCount": 1,
            },
            request_id="bootstrap_fixture_01",
        )
        assert bootstrapped.snapshot is not None
        assert bootstrapped.projection == source
        projected = client.request(
            {
                "kind": "project_markdown",
                "snapshotBase64": bootstrapped.snapshot,
                "updatesBase64": (),
                "expectedBaseStructuredHeadSha256": structured_head_sha256(
                    bootstrapped.snapshot
                ),
            },
            request_id="projection_fixture_01",
        )
        assert projected.projection == source


def test_python_rejects_source_and_base_precondition_mismatch_before_dispatch() -> None:
    with DocumentKernelClient() as client:
        with pytest.raises(KernelProtocolError) as source_error:
            client.request(
                {
                    "kind": "bootstrap_markdown",
                    "sourceBase64": b"content",
                    "sourceSha256": "0" * 64,
                    "newlineStyle": "none",
                    "utf8Bom": False,
                    "trailingNewlineCount": 0,
                }
            )
        assert source_error.value.code == "source_hash_mismatch"
        with pytest.raises(KernelProtocolError) as base_error:
            client.request(
                {
                    "kind": "project_markdown",
                    "snapshotBase64": b"snapshot",
                    "updatesBase64": (),
                    "expectedBaseStructuredHeadSha256": "0" * 64,
                }
            )
        assert base_error.value.code == "base_head_mismatch"


def test_worker_restarts_after_process_exit() -> None:
    with DocumentKernelClient() as client:
        client.health()
        first = client._process
        assert first is not None
        first.kill()
        first.wait(timeout=2)
        assert client.health()["status"] == "ready"
        assert client._process is not first


@pytest.mark.parametrize(
    ("body", "timeout", "expected"),
    [
        ("process.stdin.resume();", 0.1, KernelAmbiguousCompletion),
        (
            'process.stdin.once("data",()=>process.stdout.write("not-json\\n"));',
            2.0,
            KernelProtocolError,
        ),
    ],
)
def test_timeout_or_malformed_worker_fails_closed(
    tmp_path: Path,
    body: str,
    timeout: float,
    expected: type[Exception],
) -> None:
    runtime = tmp_path / "worker.mjs"
    runtime.write_text(body, encoding="utf-8")
    with DocumentKernelClient(runtime_path=runtime, default_timeout=timeout) as client:
        with pytest.raises(expected):
            client.health(timeout=timeout)
