"""Versioned, content-minimized protocol contracts for the Node kernel."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
from dataclasses import dataclass
from typing import Any, Mapping


PROTOCOL_VERSION = "cowork-document-kernel/v1"
RUNTIME_VERSION = "1.0.0"
SCHEMA_VERSION = "cowork-yjs/v1"
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_SEGMENT_BYTES = 64 * 1024 * 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_HEAD_DOMAIN = b"cowork-yjs-structured-head/v1\0"
_LENGTH = struct.Struct(">I")


class DocumentKernelError(RuntimeError):
    """Base content-free document-kernel failure."""

    code = "document_kernel_failed"
    retryable = False

    def __init__(self, code: str | None = None, *, retryable: bool | None = None) -> None:
        self.code = code or self.code
        if retryable is not None:
            self.retryable = retryable
        super().__init__(self.code)


class KernelUnavailable(DocumentKernelError):
    code = "document_kernel_unavailable"
    retryable = True


class KernelProtocolError(DocumentKernelError):
    code = "document_kernel_protocol_error"


class KernelAmbiguousCompletion(DocumentKernelError):
    """The worker may have completed after the caller stopped observing it."""

    code = "document_kernel_ambiguous_completion"
    retryable = True


@dataclass(frozen=True, slots=True)
class KernelOutcome:
    request_id: str
    operation_kind: str
    values: Mapping[str, Any]

    @property
    def snapshot(self) -> bytes | None:
        value = self.values.get("snapshot")
        return value if isinstance(value, bytes) else None

    @property
    def update(self) -> bytes | None:
        value = self.values.get("update")
        return value if isinstance(value, bytes) else None

    @property
    def projection(self) -> bytes | None:
        value = self.values.get("projection")
        return value if isinstance(value, bytes) else None


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def structured_head_sha256(snapshot: bytes, updates: tuple[bytes, ...] = ()) -> str:
    digest = hashlib.sha256()
    digest.update(_HEAD_DOMAIN)
    digest.update(_LENGTH.pack(len(snapshot)))
    digest.update(snapshot)
    for update in updates:
        digest.update(_LENGTH.pack(len(update)))
        digest.update(update)
    return digest.hexdigest()


def require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise KernelProtocolError(f"invalid_{label}")
    return value


_BINARY_RESULT_KEYS = frozenset({"snapshot", "update", "projection"})


def encode_operation(value: Any) -> Any:
    if isinstance(value, bytes):
        if len(value) > MAX_SEGMENT_BYTES:
            raise KernelProtocolError("segment_too_large")
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, tuple | list):
        return [encode_operation(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): encode_operation(item) for key, item in value.items()}
    return value


def decode_result(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in _BINARY_RESULT_KEYS:
            if not isinstance(item, str):
                raise KernelProtocolError("invalid_binary_result")
            try:
                decoded = base64.b64decode(item, validate=True)
            except (ValueError, TypeError) as exc:
                raise KernelProtocolError("invalid_binary_result") from exc
            if len(decoded) > MAX_SEGMENT_BYTES:
                raise KernelProtocolError("segment_too_large")
            result[key] = decoded
        else:
            result[key] = item
    return result


def canonical_request_bytes(value: Mapping[str, Any]) -> bytes:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_REQUEST_BYTES:
        raise KernelProtocolError("request_too_large")
    return payload


__all__ = [
    "DocumentKernelError",
    "KernelAmbiguousCompletion",
    "KernelOutcome",
    "KernelProtocolError",
    "KernelUnavailable",
    "MAX_REQUEST_BYTES",
    "MAX_SEGMENT_BYTES",
    "PROTOCOL_VERSION",
    "RUNTIME_VERSION",
    "SCHEMA_VERSION",
    "canonical_request_bytes",
    "decode_result",
    "encode_operation",
    "require_digest",
    "sha256_bytes",
    "structured_head_sha256",
]
