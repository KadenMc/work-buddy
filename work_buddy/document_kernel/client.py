"""Supervised request/response client for the packaged Node document kernel."""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from work_buddy.compat import subprocess_creation_flags
from work_buddy.document_kernel.protocol import (
    PROTOCOL_VERSION,
    RUNTIME_VERSION,
    SCHEMA_VERSION,
    DocumentKernelError,
    KernelAmbiguousCompletion,
    KernelOutcome,
    KernelProtocolError,
    KernelUnavailable,
    canonical_request_bytes,
    decode_result,
    encode_operation,
    require_digest,
    sha256_bytes,
    structured_head_sha256,
)


_EOF = object()


class DocumentKernelClient:
    """One sequential, restartable local worker.

    The worker accepts typed operations only. A timeout or EOF after the request
    is written is deliberately ambiguous; callers reconcile their durable
    prepared intent before retrying rather than replaying blindly.
    """

    def __init__(
        self,
        *,
        runtime_path: str | Path | None = None,
        node_binary: str | None = None,
        default_timeout: float = 15.0,
    ) -> None:
        self.runtime_path = (
            Path(runtime_path)
            if runtime_path is not None
            else Path(__file__).with_name("runtime_dist") / "worker.mjs"
        )
        self.node_binary = node_binary or shutil.which("node")
        self.default_timeout = default_timeout
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[object] = queue.Queue()
        self._lock = threading.RLock()

    def _start(self) -> subprocess.Popen[str]:
        current = self._process
        if current is not None and current.poll() is None:
            return current
        if self.node_binary is None or not self.runtime_path.is_file():
            raise KernelUnavailable()
        self._responses = queue.Queue()
        try:
            process = subprocess.Popen(
                [self.node_binary, str(self.runtime_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                creationflags=subprocess_creation_flags(),
            )
        except OSError as exc:
            raise KernelUnavailable() from exc
        self._process = process

        def _read() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    self._responses.put(line)
            finally:
                self._responses.put(_EOF)

        threading.Thread(
            target=_read,
            name="document-kernel-responses",
            daemon=True,
        ).start()
        return process

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            if process is None:
                return
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)

    def restart(self) -> None:
        self.close()
        with self._lock:
            self._start()

    def request(
        self,
        operation: Mapping[str, Any],
        *,
        timeout: float | None = None,
        request_id: str | None = None,
    ) -> KernelOutcome:
        operation_kind = operation.get("kind")
        if not isinstance(operation_kind, str):
            raise KernelProtocolError("invalid_operation")
        effective_timeout = self.default_timeout if timeout is None else timeout
        if effective_timeout <= 0:
            raise KernelProtocolError("invalid_timeout")
        identifier = request_id or f"kr_{uuid.uuid4().hex}"
        self._validate_operation_preconditions(operation)
        request = {
            "protocol": PROTOCOL_VERSION,
            "runtimeVersion": RUNTIME_VERSION,
            "schemaVersion": SCHEMA_VERSION,
            "requestId": identifier,
            "deadlineMs": int((time.time() + effective_timeout) * 1000),
            "operation": encode_operation(operation),
        }
        payload = canonical_request_bytes(request)
        with self._lock:
            process = self._start()
            assert process.stdin is not None
            try:
                process.stdin.write(payload.decode("utf-8") + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._process = None
                raise KernelAmbiguousCompletion() from exc
            try:
                line = self._responses.get(timeout=effective_timeout)
            except queue.Empty as exc:
                process.kill()
                self._process = None
                raise KernelAmbiguousCompletion() from exc
            if line is _EOF:
                self._process = None
                raise KernelAmbiguousCompletion()
            if not isinstance(line, str):
                raise KernelProtocolError()
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                raise KernelProtocolError() from exc
            return self._validate_response(
                response,
                request_id=identifier,
                operation_kind=operation_kind,
                operation=operation,
            )

    @staticmethod
    def _validate_operation_preconditions(operation: Mapping[str, Any]) -> None:
        kind = operation.get("kind")
        if kind in {
            "project_markdown",
            "apply_source_markdown",
            "replace_text",
            "validate_yjs_update",
        }:
            snapshot = operation.get("snapshotBase64")
            updates = operation.get("updatesBase64")
            if not isinstance(snapshot, bytes) or not isinstance(updates, (tuple, list)):
                raise KernelProtocolError("invalid_structured_base")
            if not all(isinstance(update, bytes) for update in updates):
                raise KernelProtocolError("invalid_structured_base")
            expected = require_digest(
                operation.get("expectedBaseStructuredHeadSha256"),
                "expectedBaseStructuredHeadSha256",
            )
            if structured_head_sha256(snapshot, tuple(updates)) != expected:
                raise KernelProtocolError("base_head_mismatch")
        if kind == "validate_yjs_update":
            update = operation.get("updateBase64")
            updates = operation.get("updatesBase64")
            snapshot = operation.get("snapshotBase64")
            if (
                not isinstance(update, bytes)
                or not isinstance(snapshot, bytes)
                or not isinstance(updates, (tuple, list))
                or not all(isinstance(item, bytes) for item in updates)
            ):
                raise KernelProtocolError("invalid_structured_update")
            result_head = structured_head_sha256(snapshot, (*updates, update))
            if result_head != require_digest(
                operation.get("expectedResultStructuredHeadSha256"),
                "expectedResultStructuredHeadSha256",
            ):
                raise KernelProtocolError("result_head_mismatch")
        if kind in {"bootstrap_markdown", "apply_source_markdown"}:
            source = operation.get("sourceBase64")
            if not isinstance(source, bytes) or sha256_bytes(source) != require_digest(
                operation.get("sourceSha256"), "sourceSha256"
            ):
                raise KernelProtocolError("source_hash_mismatch")
        if kind == "replace_text":
            copied = operation.get("copiedText")
            if not isinstance(copied, str) or sha256_bytes(copied.encode("utf-8")) != (
                require_digest(operation.get("copiedTextSha256"), "copiedTextSha256")
            ):
                raise KernelProtocolError("copied_text_hash_mismatch")

    @staticmethod
    def _validate_response(
        response: object,
        *,
        request_id: str,
        operation_kind: str,
        operation: Mapping[str, Any],
    ) -> KernelOutcome:
        if not isinstance(response, dict):
            raise KernelProtocolError()
        if (
            response.get("protocol") != PROTOCOL_VERSION
            or response.get("runtimeVersion") != RUNTIME_VERSION
            or response.get("schemaVersion") != SCHEMA_VERSION
            or response.get("requestId") != request_id
            or response.get("operationKind") != operation_kind
        ):
            raise KernelProtocolError("response_binding_mismatch")
        if response.get("ok") is not True:
            error = response.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                raise KernelProtocolError()
            raise DocumentKernelError(
                str(error["code"]),
                retryable=error.get("retryable") is True,
            )
        raw = response.get("result")
        if not isinstance(raw, dict):
            raise KernelProtocolError()
        values = decode_result(raw)
        checks = (
            ("snapshot", "resultSnapshotSha256"),
            ("update", "resultUpdateSha256"),
            ("projection", "resultProjectionSha256"),
            ("projection", "projectionSha256"),
        )
        for binary_key, digest_key in checks:
            binary = values.get(binary_key)
            declared = values.get(digest_key)
            if binary is None or declared is None:
                continue
            if not isinstance(binary, bytes):
                raise KernelProtocolError("invalid_binary_result")
            if sha256_bytes(binary) != require_digest(declared, digest_key):
                raise KernelProtocolError("result_hash_mismatch")
        expected_base = (
            "0" * 64
            if operation_kind == "bootstrap_markdown"
            else operation.get("expectedBaseStructuredHeadSha256")
        )
        if expected_base is not None and values.get("baseStructuredHeadSha256") != expected_base:
            raise KernelProtocolError("result_base_binding_mismatch")
        if operation_kind == "validate_yjs_update":
            snapshot = operation.get("snapshotBase64")
            update = operation.get("updateBase64")
            if (
                not isinstance(snapshot, bytes)
                or values.get("resultSnapshotSha256") != sha256_bytes(snapshot)
                or not isinstance(update, bytes)
                or values.get("resultUpdateSha256") != sha256_bytes(update)
                or values.get("resultStructuredHeadSha256")
                != operation.get("expectedResultStructuredHeadSha256")
            ):
                raise KernelProtocolError("result_update_binding_mismatch")
        expected_copy = (
            operation.get("sourceSha256")
            if operation_kind in {"bootstrap_markdown", "apply_source_markdown"}
            else operation.get("copiedTextSha256")
            if operation_kind == "replace_text"
            else None
        )
        if expected_copy is not None and values.get("exactCopiedTextSha256") != expected_copy:
            raise KernelProtocolError("result_source_binding_mismatch")
        manifest_digest = values.get("operationManifestSha256")
        if manifest_digest is not None:
            manifest = {
                key: value
                for key, value in values.items()
                if key not in {"snapshot", "update", "projection", "operationManifestSha256"}
            }
            encoded = json.dumps(
                manifest,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            if sha256_bytes(encoded) != require_digest(
                manifest_digest, "operationManifestSha256"
            ):
                raise KernelProtocolError("operation_manifest_mismatch")
        return KernelOutcome(request_id, operation_kind, values)

    def health(self, *, timeout: float = 5.0) -> Mapping[str, Any]:
        outcome = self.request({"kind": "health"}, timeout=timeout)
        return outcome.values

    def __enter__(self) -> "DocumentKernelClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


__all__ = [
    "DocumentKernelClient",
    "DocumentKernelError",
    "KernelAmbiguousCompletion",
    "KernelOutcome",
    "KernelProtocolError",
    "KernelUnavailable",
]
