"""First-party source locator validation for truth evidence.

The registry normalizes locator metadata and describes later integrity work.
It never resolves an identifier, reads a source file, or performs a network
request. Capture code remains responsible for storing the bytes represented by
``content_sha256``.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from work_buddy.truth.contracts import InvariantViolation


class LocatorError(InvariantViolation):
    """A source locator or its integrity metadata is invalid."""


VERIFIABILITY_CLASSES = frozenset({"A", "B", "C", "D"})
EVIDENCE_KINDS = frozenset(
    {"document", "web", "chat", "utterance", "artifact", "import"}
)

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_FILE_URI_RE = re.compile(r"^/[A-Za-z]:/")
_BAD_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_SWH_CORE_RE = re.compile(
    r"^swh:1:(cnt|dir|rev|rel|snp):([0-9A-Fa-f]{40})$",
    re.IGNORECASE,
)
_SWH_QUALIFIER_ORDER = ("origin", "visit", "anchor", "path", "lines")
_SWH_ALLOWED_QUALIFIERS = frozenset(_SWH_QUALIFIER_ORDER)
_SWH_ANCHOR_TYPES = frozenset({"dir", "rev", "rel", "snp"})
_LINES_RE = re.compile(r"^0*([1-9][0-9]*)(?:-0*([1-9][0-9]*))?$")
_DOI_RE = re.compile(r"^10\.[0-9]{4,9}/[-._;()/:A-Z0-9]+$", re.IGNORECASE)
_ARXIV_MODERN_RE = re.compile(r"^[0-9]{4}\.[0-9]{4,5}(?:v[1-9][0-9]*)?$", re.IGNORECASE)
_ARXIV_LEGACY_RE = re.compile(
    r"^[a-z][a-z0-9.-]*/[0-9]{7}(?:v[1-9][0-9]*)?$",
    re.IGNORECASE,
)
_PMID_RE = re.compile(r"^[1-9][0-9]*$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]*$")


def _stable_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key in value:
            if not isinstance(key, str):
                raise LocatorError("locator metadata keys must be strings")
        for key in sorted(value):
            normalized[key] = _stable_value(value[key])
        return normalized
    if isinstance(value, list):
        return [_stable_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_stable_value(item) for item in value)
    return copy.deepcopy(value)


def _stable_mapping(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LocatorError(f"{label} must be a mapping")
    normalized = _stable_value(value)
    if not isinstance(normalized, dict):  # pragma: no cover
        raise LocatorError(f"{label} must be a mapping")
    try:
        json.dumps(normalized, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise LocatorError(f"{label} must contain JSON-compatible values") from exc
    return normalized


def _normalize_sha256(value: str | None, label: str = "content_sha256") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LocatorError(f"{label} must be a hexadecimal string")
    normalized = value.strip().lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise LocatorError(f"{label} must be a 64-character SHA-256 digest")
    return normalized


def _require_sha256(value: str | None, label: str = "content_sha256") -> str:
    normalized = _normalize_sha256(value, label)
    if normalized is None:
        raise LocatorError(f"{label} is required")
    return normalized


def _normalize_scheme(value: str) -> str:
    if not isinstance(value, str):
        raise LocatorError("locator scheme must be a string")
    normalized = value.strip().lower()
    if _SCHEME_RE.fullmatch(normalized) is None:
        raise LocatorError(f"invalid locator scheme {value!r}")
    return normalized


def _normalize_kind(value: str) -> str:
    if not isinstance(value, str):
        raise LocatorError("evidence kind must be a string")
    normalized = value.strip().lower()
    if normalized not in EVIDENCE_KINDS:
        raise LocatorError(f"evidence kind must be one of {sorted(EVIDENCE_KINDS)}")
    return normalized


def _require_locator(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LocatorError("source locator must be a nonempty string")
    normalized = value.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise LocatorError("source locator cannot contain control characters")
    return normalized


def _decode_component(value: str, label: str) -> str:
    if _BAD_PERCENT_RE.search(value):
        raise LocatorError(f"{label} contains an invalid percent escape")
    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise LocatorError(f"{label} contains invalid UTF-8 escaping") from exc
    if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        raise LocatorError(f"{label} cannot contain control characters")
    return decoded


def _quote_uri_component(value: str, safe: str) -> str:
    if _BAD_PERCENT_RE.search(value):
        raise LocatorError("URI contains an invalid percent escape")
    encoded = quote(value, safe=safe, encoding="utf-8", errors="strict")
    return re.sub(
        r"%[0-9a-fA-F]{2}",
        lambda match: match.group(0).upper(),
        encoded,
    )


def _reject_traversal(path: str, label: str) -> None:
    if "\\" in path:
        raise LocatorError(f"{label} must use forward slashes")
    segments = path.split("/")
    if any(segment in {".", ".."} for segment in segments):
        raise LocatorError(f"{label} cannot contain path traversal segments")


def _normalize_hostname(hostname: str, label: str) -> str:
    if not hostname:
        raise LocatorError(f"{label} requires a host")
    if (
        hostname in {".", ".."}
        or "%" in hostname
        or "/" in hostname
        or "\\" in hostname
        or any(character.isspace() for character in hostname)
    ):
        raise LocatorError(f"{label} contains an invalid host")
    if ":" in hostname:
        return f"[{hostname.lower()}]"
    try:
        normalized = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise LocatorError(f"{label} contains an invalid international host") from exc
    labels = normalized.rstrip(".").split(".")
    if not labels or any(not item for item in labels):
        raise LocatorError(f"{label} contains an invalid host")
    return normalized


def _normalize_http_url(value: str, label: str = "web locator") -> str:
    raw = _require_locator(value)
    if "\\" in raw:
        raise LocatorError(f"{label} cannot contain backslashes")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise LocatorError(f"{label} is malformed") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc:
        raise LocatorError(f"{label} must be an absolute HTTP or HTTPS URI")
    if parsed.username is not None or parsed.password is not None:
        raise LocatorError(f"{label} cannot embed credentials")
    host = _normalize_hostname(parsed.hostname or "", label)
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    path = _quote_uri_component(
        parsed.path or "/",
        safe="/%:@-._~!$&'()*+,;=",
    )
    query = _quote_uri_component(
        parsed.query,
        safe="=&/?%:@-._~!$'()*+,;",
    )
    fragment = _quote_uri_component(
        parsed.fragment,
        safe="/?%:@-._~!$&'()*+,;=",
    )
    return urlunsplit((scheme, netloc, path, query, fragment))


def _normalize_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LocatorError(f"{label} must be a nonempty ISO 8601 timestamp")
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(
            raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        )
    except ValueError as exc:
        raise LocatorError(f"{label} must be a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LocatorError(f"{label} must include a UTC offset")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _normalize_file_path(path: str, label: str) -> str:
    if not path:
        raise LocatorError(f"{label} requires an absolute path")
    decoded = _decode_component(path, label)
    _reject_traversal(decoded, label)
    if "//" in decoded[1:]:
        raise LocatorError(f"{label} cannot contain empty path segments")
    if decoded.endswith("/"):
        raise LocatorError(f"{label} must identify a file")
    return _quote_uri_component(decoded, safe="/:@-._~!$&'()*+,=")


def _normalize_file_locator(value: str) -> str:
    raw = _require_locator(value)
    if _WINDOWS_ABSOLUTE_RE.match(raw):
        normalized_path = raw.replace("\\", "/")
        drive = normalized_path[0].upper()
        path = _normalize_file_path(f"/{drive}{normalized_path[1:]}", "file path")
        return f"file://{path}"
    if raw.startswith("\\\\"):
        unc = raw[2:].replace("\\", "/")
        parts = unc.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise LocatorError("UNC file path requires a server, share, and file")
        host = _normalize_hostname(parts[0], "UNC file path")
        path = _normalize_file_path(f"/{parts[1]}", "UNC file path")
        return f"file://{host}{path}"
    if raw.startswith("/") and not raw.startswith("//"):
        path = _normalize_file_path(raw, "file path")
        return f"file://{path}"

    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise LocatorError("file locator is malformed") from exc
    if parsed.scheme.lower() != "file" or parsed.query or parsed.fragment:
        raise LocatorError("file locator must be an absolute path or file URI")
    if parsed.username is not None or parsed.password is not None:
        raise LocatorError("file locator cannot embed credentials")
    if "\\" in parsed.path:
        raise LocatorError("file URI must use forward slashes")

    host = parsed.hostname or ""
    if host.lower() == "localhost":
        host = ""
    if host:
        normalized_host = _normalize_hostname(host, "file URI")
        path = _normalize_file_path(parsed.path, "file URI path")
        if path == "/":
            raise LocatorError("file URI must identify a file")
        return f"file://{normalized_host}{path}"

    if parsed.path.startswith("//"):
        raise LocatorError("UNC file URI must put the server in its authority")
    path = _normalize_file_path(parsed.path, "file URI path")
    if not path.startswith("/"):
        raise LocatorError("file URI path must be absolute")
    if len(path) >= 4 and path[2] == ":" and _WINDOWS_FILE_URI_RE.match(path):
        path = f"/{path[1].upper()}{path[2:]}"
    return f"file://{path}"


def _normalize_archive_uri(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LocatorError("archived_uri must be a nonempty absolute URI")
    raw = value.strip()
    scheme = _detect_scheme(raw)
    if scheme in {"http", "https"}:
        return _normalize_http_url(raw, "archived_uri")
    if scheme == "file" or _looks_like_absolute_path(raw):
        return _normalize_file_locator(raw)
    raise LocatorError("archived_uri must use HTTP, HTTPS, or file")


def _normalize_origin_uri(value: str) -> str:
    raw = _require_locator(value)
    scheme = _detect_scheme(raw)
    if scheme in {"http", "https"}:
        return _normalize_http_url(raw, "SWHID origin")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise LocatorError("SWHID origin is malformed") from exc
    if not parsed.scheme or not (parsed.netloc or parsed.path):
        raise LocatorError("SWHID origin must be an absolute URI")
    return f"{parsed.scheme.lower()}:{raw.split(':', 1)[1]}"


def _looks_like_absolute_path(value: str) -> bool:
    return bool(
        _WINDOWS_ABSOLUTE_RE.match(value)
        or value.startswith("/")
        or value.startswith("\\\\")
    )


def _detect_scheme(value: str) -> str:
    if _looks_like_absolute_path(value):
        return "file"
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):", value)
    if match is None:
        raise LocatorError("source locator must contain a registered URI scheme")
    return _normalize_scheme(match.group(1))


def _normalize_pinpoint(meta: Mapping[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if "pinpoint" in meta:
        pinpoint = meta["pinpoint"]
        if not isinstance(pinpoint, Mapping):
            raise LocatorError("pinpoint must be a mapping")
        label = pinpoint.get("label")
        locator = pinpoint.get("locator")
        if not isinstance(label, str) or not label.strip():
            raise LocatorError("pinpoint.label must be a nonempty string")
        if not isinstance(locator, str) or not locator.strip():
            raise LocatorError("pinpoint.locator must be a nonempty string")
        normalized_pinpoint = _stable_mapping(pinpoint, "pinpoint")
        normalized_pinpoint["label"] = label.strip().lower()
        normalized_pinpoint["locator"] = locator.strip()
        updates["pinpoint"] = _stable_mapping(normalized_pinpoint, "pinpoint")

    csl_locator = meta.get("csl_locator")
    csl_label = meta.get("csl_label")
    if (csl_locator is None) != (csl_label is None):
        raise LocatorError("csl_locator and csl_label must be supplied together")
    if csl_locator is not None:
        if not isinstance(csl_locator, str) or not csl_locator.strip():
            raise LocatorError("csl_locator must be a nonempty string")
        if not isinstance(csl_label, str) or not csl_label.strip():
            raise LocatorError("csl_label must be a nonempty string")
        updates["csl_locator"] = csl_locator.strip()
        updates["csl_label"] = csl_label.strip().lower()
        if "pinpoint" in updates and (
            updates["pinpoint"]["locator"] != updates["csl_locator"]
            or updates["pinpoint"]["label"] != updates["csl_label"]
        ):
            raise LocatorError("pinpoint and CSL pinpoint metadata do not match")
    return updates


def _snapshot_updates(
    meta: Mapping[str, Any],
    content_sha256: str | None,
) -> dict[str, Any]:
    declared = meta.get("snapshot_sha256")
    if declared is not None:
        declared_digest = _require_sha256(declared, "snapshot_sha256")
        if content_sha256 is None:
            raise LocatorError("snapshot_sha256 requires captured content_sha256")
        if declared_digest != content_sha256:
            raise LocatorError("snapshot_sha256 does not match content_sha256")
    return {"snapshot_sha256": content_sha256} if content_sha256 is not None else {}


@dataclass(frozen=True, slots=True)
class SchemeValidation:
    """The normalized output returned by a registered scheme validator."""

    locator: str
    verifiability_class: str
    integrity_recipe: Mapping[str, Any]
    meta_updates: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LocatorValidation(Mapping[str, Any]):
    """A normalized locator plus its deterministic verification contract."""

    kind: str
    locator: str
    locator_scheme: str
    content_sha256: str | None
    verifiability_class: str
    integrity_recipe: Mapping[str, Any]
    meta: Mapping[str, Any]

    _KEYS = (
        "kind",
        "locator",
        "locator_scheme",
        "content_sha256",
        "verifiability_class",
        "integrity_recipe",
        "meta",
    )

    def __getitem__(self, key: str) -> Any:
        if key not in self._KEYS:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._KEYS)

    def __len__(self) -> int:
        return len(self._KEYS)

    def to_dict(self) -> dict[str, Any]:
        """Return a stable plain mapping suitable for JSON serialization."""
        return {
            "kind": self.kind,
            "locator": self.locator,
            "locator_scheme": self.locator_scheme,
            "content_sha256": self.content_sha256,
            "verifiability_class": self.verifiability_class,
            "integrity_recipe": _stable_mapping(
                self.integrity_recipe, "integrity_recipe"
            ),
            "meta": _stable_mapping(self.meta, "locator metadata"),
        }


SchemeValidator = Callable[
    [str, str, Mapping[str, Any], str | None],
    SchemeValidation,
]


@dataclass(frozen=True, slots=True)
class _Registration:
    kinds: frozenset[str]
    validator: SchemeValidator


def _validate_swh(
    kind: str,
    locator: str,
    meta: Mapping[str, Any],
    content_sha256: str | None,
) -> SchemeValidation:
    del kind
    pieces = locator.split(";")
    core_match = _SWH_CORE_RE.fullmatch(pieces[0])
    if core_match is None:
        raise LocatorError("SWHID core must use swh:1:TYPE with a 40-hex hash")
    object_type, object_hash = core_match.groups()
    object_type = object_type.lower()
    object_hash = object_hash.lower()
    if object_type != "cnt":
        raise LocatorError("Git-sourced evidence requires an swh:1:cnt SWHID")

    qualifiers: dict[str, str] = {}
    for piece in pieces[1:]:
        if "=" not in piece:
            raise LocatorError("each SWHID qualifier must use name=value")
        name, value = piece.split("=", 1)
        name = name.strip().lower()
        if name not in _SWH_ALLOWED_QUALIFIERS:
            raise LocatorError(f"unsupported SWHID qualifier {name!r}")
        if name in qualifiers:
            raise LocatorError(f"duplicate SWHID qualifier {name!r}")
        if not value:
            raise LocatorError(f"SWHID qualifier {name!r} cannot be empty")
        qualifiers[name] = value

    missing = [
        name for name in ("origin", "anchor", "path", "lines") if name not in qualifiers
    ]
    if missing:
        raise LocatorError(
            "qualified content SWHID requires origin, anchor, path, and lines"
        )
    qualifiers["origin"] = _normalize_origin_uri(qualifiers["origin"])

    if "visit" in qualifiers:
        visit_match = _SWH_CORE_RE.fullmatch(qualifiers["visit"])
        if visit_match is None or visit_match.group(1).lower() != "snp":
            raise LocatorError("SWHID visit must be an unqualified snapshot SWHID")
        qualifiers["visit"] = f"swh:1:snp:{visit_match.group(2).lower()}"

    anchor_match = _SWH_CORE_RE.fullmatch(qualifiers["anchor"])
    if anchor_match is None or anchor_match.group(1).lower() not in _SWH_ANCHOR_TYPES:
        raise LocatorError(
            "SWHID anchor must be an unqualified dir, rev, rel, or snp SWHID"
        )
    qualifiers["anchor"] = (
        f"swh:1:{anchor_match.group(1).lower()}:{anchor_match.group(2).lower()}"
    )

    path = _decode_component(qualifiers["path"], "SWHID path")
    if not path.startswith("/") or path.endswith("/"):
        raise LocatorError("SWHID path must be an absolute file path")
    _reject_traversal(path, "SWHID path")
    qualifiers["path"] = _quote_uri_component(
        path,
        safe="/:@-._~!$&'()*+,=",
    )

    line_range: list[int] | None = None
    if "lines" in qualifiers:
        lines_match = _LINES_RE.fullmatch(qualifiers["lines"])
        if lines_match is None:
            raise LocatorError("SWHID lines must be START or START-END")
        start = int(lines_match.group(1))
        end = int(lines_match.group(2) or lines_match.group(1))
        if end < start:
            raise LocatorError("SWHID line range cannot end before it starts")
        qualifiers["lines"] = str(start) if start == end else f"{start}-{end}"
        line_range = [start, end]

    core = f"swh:1:cnt:{object_hash}"
    normalized_locator = core + "".join(
        f";{name}={qualifiers[name]}"
        for name in _SWH_QUALIFIER_ORDER
        if name in qualifiers
    )
    recipe: dict[str, Any] = {
        "algorithm": "git-blob-sha1",
        "expected_digest": object_hash,
        "line_range": line_range,
        "method": "recompute_swhid_content_hash",
        "network_required": False,
        "qualifiers": {
            name: qualifiers[name]
            for name in _SWH_QUALIFIER_ORDER
            if name in qualifiers
        },
    }
    if content_sha256 is not None:
        recipe["snapshot_sha256"] = content_sha256
    permalink_template = meta.get("permalink_template")
    if not isinstance(permalink_template, str) or not permalink_template.strip():
        raise LocatorError(
            "Git-sourced evidence requires a nonempty permalink_template"
        )
    return SchemeValidation(normalized_locator, "A", recipe)


def _validate_web(
    kind: str,
    locator: str,
    meta: Mapping[str, Any],
    content_sha256: str | None,
) -> SchemeValidation:
    del kind
    normalized_locator = _normalize_http_url(locator)
    retrieved_at = meta.get("retrieved_at")
    retrieved_datetime = meta.get("retrieved_datetime")
    if retrieved_at is None and retrieved_datetime is None:
        raise LocatorError("web locator requires retrieved_at retrieval state")
    normalized_retrieved = _normalize_timestamp(
        retrieved_at if retrieved_at is not None else retrieved_datetime,
        "retrieved_at",
    )
    if retrieved_at is not None and retrieved_datetime is not None:
        alias_value = _normalize_timestamp(retrieved_datetime, "retrieved_datetime")
        if alias_value != normalized_retrieved:
            raise LocatorError("retrieved_at and retrieved_datetime do not match")

    if "http_request_state" in meta and not isinstance(
        meta["http_request_state"], Mapping
    ):
        raise LocatorError("http_request_state must be a mapping")

    updates: dict[str, Any] = {"retrieved_at": normalized_retrieved}
    updates.update(_snapshot_updates(meta, content_sha256))
    archived_uri: str | None = None
    if meta.get("archived_uri") is not None:
        archived_uri = _normalize_archive_uri(meta["archived_uri"])
        if archived_uri == normalized_locator:
            raise LocatorError("archived_uri must differ from the live locator")
        if archived_uri.startswith("file:") and content_sha256 is None:
            raise LocatorError("a local archived_uri requires captured content_sha256")
        updates["archived_uri"] = archived_uri

    if archived_uri is not None or content_sha256 is not None:
        recipe = {
            "archived_uri": archived_uri,
            "expected_sha256": content_sha256,
            "method": "verify_web_snapshot",
            "network_required": bool(
                archived_uri is not None
                and archived_uri.startswith(("http://", "https://"))
                and content_sha256 is None
            ),
            "retrieved_at": normalized_retrieved,
        }
        return SchemeValidation(normalized_locator, "B", recipe, updates)

    recipe = {
        "method": "check_live_url_and_capture_drift",
        "network_required": True,
        "retrieved_at": normalized_retrieved,
    }
    return SchemeValidation(normalized_locator, "D", recipe, updates)


def _academic_identifier(scheme: str, locator: str) -> tuple[str, str]:
    prefix, separator, raw_identifier = locator.partition(":")
    if not separator or prefix.lower() != scheme:
        raise LocatorError(f"{scheme} locator must use the {scheme}: scheme")
    identifier = _decode_component(raw_identifier.strip(), f"{scheme} identifier")
    if not identifier or any(character.isspace() for character in identifier):
        raise LocatorError(f"{scheme} identifier cannot be empty or contain whitespace")
    if "?" in identifier or "#" in identifier:
        raise LocatorError(f"{scheme} pinpoint belongs in metadata, not the locator")

    if scheme == "doi":
        identifier = identifier.lower()
        if _DOI_RE.fullmatch(identifier) is None:
            raise LocatorError("DOI must use doi:10.REGISTRANT/SUFFIX")
        resolver_uri = f"https://doi.org/{identifier}"
    elif scheme == "arxiv":
        identifier = identifier.lower()
        if (
            _ARXIV_MODERN_RE.fullmatch(identifier) is None
            and _ARXIV_LEGACY_RE.fullmatch(identifier) is None
        ):
            raise LocatorError("arXiv identifier is malformed")
        resolver_uri = f"https://arxiv.org/abs/{identifier}"
    else:
        if _PMID_RE.fullmatch(identifier) is None:
            raise LocatorError("PMID must contain positive decimal digits")
        resolver_uri = f"https://pubmed.ncbi.nlm.nih.gov/{identifier}/"
    return identifier, resolver_uri


def _validate_academic(
    kind: str,
    locator: str,
    meta: Mapping[str, Any],
    content_sha256: str | None,
) -> SchemeValidation:
    del kind
    scheme = _detect_scheme(locator)
    identifier, resolver_uri = _academic_identifier(scheme, locator)
    if "csl_json" not in meta:
        raise LocatorError("academic evidence requires csl_json metadata")
    if not isinstance(meta["csl_json"], Mapping):
        raise LocatorError("csl_json must be a mapping")
    updates = _normalize_pinpoint(meta)
    updates.update(_snapshot_updates(meta, content_sha256))

    if content_sha256 is not None:
        recipe = {
            "expected_sha256": content_sha256,
            "identifier": identifier,
            "method": "verify_academic_snapshot",
            "network_required": False,
            "resolver_uri": resolver_uri,
        }
        verifiability_class = "B"
    else:
        recipe = {
            "identifier": identifier,
            "match_csl_json": "csl_json" in meta,
            "method": "resolve_academic_identifier",
            "network_required": True,
            "resolver_uri": resolver_uri,
        }
        verifiability_class = "C"
    return SchemeValidation(
        f"{scheme}:{identifier}",
        verifiability_class,
        recipe,
        updates,
    )


def _validate_session(
    kind: str,
    locator: str,
    meta: Mapping[str, Any],
    content_sha256: str | None,
) -> SchemeValidation:
    del kind
    digest = _require_sha256(content_sha256)
    try:
        parsed = urlsplit(locator)
    except ValueError as exc:
        raise LocatorError("wb-session locator is malformed") from exc
    if parsed.scheme.lower() != "wb-session" or not parsed.netloc:
        raise LocatorError(
            "chat and utterance locators must use wb-session://session/message_ref"
        )
    if parsed.query or parsed.fragment or "@" in parsed.netloc or ":" in parsed.netloc:
        raise LocatorError("wb-session locator cannot contain authority or query data")
    if _SESSION_RE.fullmatch(parsed.netloc) is None:
        raise LocatorError("wb-session session id is not path safe")
    encoded_parts = parsed.path.split("/")
    if len(encoded_parts) != 2 or encoded_parts[0] or not encoded_parts[1]:
        raise LocatorError("wb-session locator requires exactly one message_ref")
    message_ref = _decode_component(encoded_parts[1], "wb-session message_ref")
    if (
        not message_ref
        or message_ref in {".", ".."}
        or "/" in message_ref
        or "\\" in message_ref
        or any(character.isspace() for character in message_ref)
    ):
        raise LocatorError("wb-session message_ref is not path safe")
    normalized_ref = _quote_uri_component(message_ref, safe="-._~:@")

    declared_digest = meta.get("transcript_sha256")
    if declared_digest is not None:
        normalized_declared = _require_sha256(declared_digest, "transcript_sha256")
        if normalized_declared != digest:
            raise LocatorError("transcript_sha256 does not match content_sha256")
    updates = {"transcript_sha256": digest}
    recipe = {
        "expected_sha256": digest,
        "message_ref": message_ref,
        "method": "verify_transcript_snapshot",
        "network_required": False,
        "session_id": parsed.netloc,
    }
    return SchemeValidation(
        f"wb-session://{parsed.netloc}/{normalized_ref}",
        "B",
        recipe,
        updates,
    )


def _validate_file(
    kind: str,
    locator: str,
    meta: Mapping[str, Any],
    content_sha256: str | None,
) -> SchemeValidation:
    del kind
    digest = _require_sha256(content_sha256)
    normalized_locator = _normalize_file_locator(locator)
    updates = _snapshot_updates(meta, digest)
    recipe = {
        "expected_sha256": digest,
        "method": "verify_local_snapshot_bytes",
        "network_required": False,
        "requires_snapshot_bytes": True,
    }
    return SchemeValidation(normalized_locator, "A", recipe, updates)


class LocatorRegistry:
    """Extensible, fail-closed registry of source locator schemes."""

    def __init__(self, *, include_builtins: bool = True) -> None:
        self._registrations: dict[str, _Registration] = {}
        if include_builtins:
            self._register_builtins()

    @property
    def schemes(self) -> tuple[str, ...]:
        """Return registered schemes in stable order."""
        return tuple(sorted(self._registrations))

    def register(
        self,
        scheme: str,
        validator: SchemeValidator,
        *,
        kinds: Iterable[str],
    ) -> None:
        """Register one validator with explicit compatible evidence kinds.

        The ``import`` evidence kind remains broad and can use every registered
        scheme. Other kinds must be listed here.
        """
        normalized_scheme = _normalize_scheme(scheme)
        if normalized_scheme in self._registrations:
            raise LocatorError(f"locator scheme {normalized_scheme!r} is registered")
        if not callable(validator):
            raise LocatorError("locator scheme validator must be callable")
        try:
            normalized_kinds = frozenset(_normalize_kind(kind) for kind in kinds)
        except TypeError as exc:
            raise LocatorError("locator scheme kinds must be iterable") from exc
        if not normalized_kinds:
            raise LocatorError("locator scheme must declare compatible evidence kinds")
        self._registrations[normalized_scheme] = _Registration(
            kinds=normalized_kinds,
            validator=validator,
        )

    def _register_builtins(self) -> None:
        self.register("swh", _validate_swh, kinds={"document", "artifact"})
        self.register("http", _validate_web, kinds={"web"})
        self.register("https", _validate_web, kinds={"web"})
        self.register("doi", _validate_academic, kinds={"document"})
        self.register("arxiv", _validate_academic, kinds={"document"})
        self.register("pmid", _validate_academic, kinds={"document"})
        self.register("wb-session", _validate_session, kinds={"chat", "utterance"})
        self.register("file", _validate_file, kinds={"document", "artifact"})

    def validate(
        self,
        kind: str,
        locator: str,
        meta: Mapping[str, Any] | None = None,
        content_sha256: str | None = None,
    ) -> LocatorValidation:
        """Validate and normalize one source locator without external I/O."""
        normalized_kind = _normalize_kind(kind)
        raw_locator = _require_locator(locator)
        scheme = _detect_scheme(raw_locator)
        registration = self._registrations.get(scheme)
        if registration is None:
            raise LocatorError(f"unregistered locator scheme {scheme!r}")
        if normalized_kind != "import" and normalized_kind not in registration.kinds:
            raise LocatorError(
                f"locator scheme {scheme!r} is incompatible with evidence kind "
                f"{normalized_kind!r}"
            )

        normalized_meta = _stable_mapping(
            {} if meta is None else meta,
            "locator metadata",
        )
        declared_scheme = normalized_meta.get("locator_scheme")
        if declared_scheme is not None:
            if not isinstance(declared_scheme, str):
                raise LocatorError("meta locator_scheme must be a string")
            if _normalize_scheme(declared_scheme) != scheme:
                raise LocatorError(
                    "meta locator_scheme does not match the source locator"
                )
        normalized_digest = _normalize_sha256(content_sha256)
        validation = registration.validator(
            normalized_kind,
            raw_locator,
            normalized_meta,
            normalized_digest,
        )
        if not isinstance(validation, SchemeValidation):
            raise LocatorError("scheme validator must return SchemeValidation")
        normalized_locator = _require_locator(validation.locator)
        if _detect_scheme(normalized_locator) != scheme:
            raise LocatorError("scheme validator changed the registered locator scheme")
        if validation.verifiability_class not in VERIFIABILITY_CLASSES:
            raise LocatorError(
                "scheme validator returned an invalid verifiability class"
            )

        recipe = _stable_mapping(validation.integrity_recipe, "integrity_recipe")
        if not isinstance(recipe.get("method"), str) or not recipe["method"].strip():
            raise LocatorError("integrity_recipe requires a nonempty method")
        if not isinstance(recipe.get("network_required"), bool):
            raise LocatorError(
                "integrity_recipe requires network_required as a boolean"
            )
        updates = _stable_mapping(validation.meta_updates, "locator metadata updates")
        update_scheme = updates.get("locator_scheme")
        if update_scheme is not None and (
            not isinstance(update_scheme, str)
            or _normalize_scheme(update_scheme) != scheme
        ):
            raise LocatorError("scheme validator returned mismatched locator_scheme")
        normalized_meta.update(updates)
        normalized_meta["locator_scheme"] = scheme
        normalized_meta = _stable_mapping(normalized_meta, "locator metadata")

        return LocatorValidation(
            kind=normalized_kind,
            locator=normalized_locator,
            locator_scheme=scheme,
            content_sha256=normalized_digest,
            verifiability_class=validation.verifiability_class,
            integrity_recipe=recipe,
            meta=normalized_meta,
        )


DEFAULT_LOCATOR_REGISTRY = LocatorRegistry()


def validate_locator(
    kind: str,
    locator: str,
    meta: Mapping[str, Any] | None = None,
    content_sha256: str | None = None,
) -> LocatorValidation:
    """Validate a locator through the default first-party registry."""
    return DEFAULT_LOCATOR_REGISTRY.validate(
        kind,
        locator,
        meta,
        content_sha256,
    )


__all__ = [
    "DEFAULT_LOCATOR_REGISTRY",
    "EVIDENCE_KINDS",
    "LocatorError",
    "LocatorRegistry",
    "LocatorValidation",
    "SchemeValidation",
    "VERIFIABILITY_CLASSES",
    "validate_locator",
]
