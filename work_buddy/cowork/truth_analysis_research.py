"""Bounded external research for one Co-work Truth-analysis worker run.

Search results are *leads*, never evidence.  A lead becomes eligible for later
evidence review only after :func:`fetch` retrieves the admitted URL through the
public-network guard and stores the exact resulting text in a run-owned fetch
receipt.  This module deliberately accepts no caller-supplied fetch URL.

The existing :mod:`truth_analysis_runtime` search/fetch tables remain the
public durable projection consumed by coverage and output validation.  A small
write-ahead table makes outbound calls replay-safe, while the acquisition table
preserves redirect and response metadata that the runtime projection does not
otherwise carry.
"""

from __future__ import annotations

import json
import http.client
import math
import re
import socket
import sqlite3
import ssl
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any, Literal
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from work_buddy.cowork import truth_analysis_runtime
from work_buddy.cowork.truth_analysis_runtime import TruthAnalysisRuntimeRun
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.identity import canonical_json, new_id, sha256_text, utc_now
from work_buddy.websearch.models import SearchHit


MAX_QUERIES_PER_RUN = 3
MAX_HITS_PER_QUERY = 5
MAX_FETCHES_PER_RUN = 5
MAX_QUERY_CHARS = 500
MAX_URL_CHARS = 4_096
MAX_RESPONSE_BYTES = 512 * 1024
MAX_CAPTURED_TEXT_BYTES = 64 * 1024
MAX_REDIRECTS = 5
MAX_TOTAL_FETCH_SECONDS = 20.0
MAX_REQUEST_SECONDS = 10.0
PENDING_OPERATION_TTL_SECONDS = 120.0

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ACTIVE_RUN_STATUSES = frozenset({"prepared", "launching", "running"})
_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".lan")
_CONTROL_OR_SPACE = re.compile(r"[\x00-\x20\x7f]")
_CHARSET = re.compile(r"charset\s*=\s*[\"']?([^;\s\"']+)", re.IGNORECASE)
_SCHEMA_LOCK = threading.RLock()

SearchCallable = Callable[..., Sequence[SearchHit]]
Resolver = Callable[[str, int], Sequence[str]]
Requester = Callable[[str, Sequence[str], float, int], "ResearchHttpResponse"]


class TruthAnalysisResearchError(InvariantViolation):
    """Typed, user-safe broker failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class ResearchSearchLead:
    """One admitted search lead.  Its snippet is never supporting evidence."""

    hit_id: str
    title: str
    url: str
    snippet: str
    provider: str
    published: str | None
    score: float | None
    rank: int
    lead_only: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ResearchSearchLead":
        score = value.get("score")
        return cls(
            hit_id=str(value.get("hit_id") or ""),
            title=str(value.get("title") or ""),
            url=str(value.get("url") or ""),
            snippet=str(value.get("snippet") or ""),
            provider=str(value.get("provider") or ""),
            published=(
                None if value.get("published") is None else str(value["published"])
            ),
            score=(
                float(score)
                if isinstance(score, (int, float)) and not isinstance(score, bool)
                else None
            ),
            rank=int(value.get("rank") or 0),
            lead_only=True,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResearchSearchResult:
    search_id: str
    run_id: str
    query: str
    status: Literal["completed", "failed"]
    hits: tuple[ResearchSearchLead, ...]
    external_egress: bool
    error_code: str
    error: str
    searched_at: str
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "search_id": self.search_id,
            "run_id": self.run_id,
            "query": self.query,
            "status": self.status,
            "hits": [item.to_dict() for item in self.hits],
            "external_egress": self.external_egress,
            "error_code": self.error_code or None,
            "error": self.error or None,
            "searched_at": self.searched_at,
            "replayed": self.replayed,
        }


@dataclass(frozen=True, slots=True)
class ResearchFetchReceipt:
    fetch_id: str
    run_id: str
    hit_id: str
    status: Literal["completed", "unavailable", "failed"]
    requested_url: str
    source_url: str
    title: str
    exact_text: str
    text_sha256: str
    media_type: str
    http_status: int | None
    extractor: str
    redirect_chain: tuple[str, ...]
    acquisition_metadata: Mapping[str, Any]
    external_egress: bool
    error_code: str
    error: str
    acquired_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetch_id": self.fetch_id,
            "run_id": self.run_id,
            "hit_id": self.hit_id,
            "status": self.status,
            "requested_url": self.requested_url,
            "source_url": self.source_url,
            "title": self.title,
            "exact_text": self.exact_text,
            "text_sha256": self.text_sha256,
            "media_type": self.media_type,
            "http_status": self.http_status,
            "extractor": self.extractor,
            "redirect_chain": list(self.redirect_chain),
            "acquisition_metadata": dict(self.acquisition_metadata),
            "external_egress": self.external_egress,
            "error_code": self.error_code or None,
            "error": self.error or None,
            "acquired_at": self.acquired_at,
        }


@dataclass(frozen=True, slots=True)
class ResearchFetchResult:
    receipt: ResearchFetchReceipt
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return {**self.receipt.to_dict(), "replayed": self.replayed}


@dataclass(frozen=True, slots=True)
class ResearchHttpResponse:
    """Fully read, bounded response returned by the injectable HTTP seam."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class _ValidatedDestination:
    url: str
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _FetchedPage:
    requested_url: str
    source_url: str
    text: str
    media_type: str
    http_status: int
    extractor: str
    redirect_chain: tuple[str, ...]
    bytes_received: int
    extracted_text_bytes: int
    captured_text_bytes: int
    full_text_sha256: str
    text_truncated: bool


def _connect() -> sqlite3.Connection:
    target = truth_analysis_runtime._DB_PATH.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Install only the broker's write-ahead and acquisition companions."""

    with _SCHEMA_LOCK:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cowork_truth_analysis_research_operations (
                operation_id  TEXT PRIMARY KEY,
                run_id        TEXT NOT NULL
                    REFERENCES cowork_truth_analysis_runs(run_id),
                kind          TEXT NOT NULL CHECK(kind IN ('search', 'fetch')),
                operation_key TEXT NOT NULL,
                state         TEXT NOT NULL
                    CHECK(state IN (
                        'pending', 'prepared', 'completed', 'outcome_unknown'
                    )),
                payload_json  TEXT NOT NULL DEFAULT '{}',
                receipt_id    TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                UNIQUE(run_id, kind, operation_key)
            );

            CREATE INDEX IF NOT EXISTS idx_truth_analysis_research_ops_run
            ON cowork_truth_analysis_research_operations(run_id, kind, created_at);

            CREATE TABLE IF NOT EXISTS cowork_truth_analysis_fetch_acquisitions (
                fetch_id          TEXT PRIMARY KEY,
                run_id            TEXT NOT NULL
                    REFERENCES cowork_truth_analysis_runs(run_id),
                hit_id            TEXT NOT NULL,
                requested_url     TEXT NOT NULL,
                source_url        TEXT NOT NULL,
                title             TEXT NOT NULL,
                exact_text        TEXT NOT NULL,
                text_sha256       TEXT NOT NULL,
                media_type        TEXT NOT NULL,
                http_status       INTEGER NOT NULL,
                extractor         TEXT NOT NULL,
                redirect_chain_json TEXT NOT NULL,
                acquisition_json  TEXT NOT NULL,
                acquired_at       TEXT NOT NULL,
                UNIQUE(run_id, hit_id)
            );
            """
        )
        conn.commit()


def _json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise TruthAnalysisResearchError(
            "research_receipt_corrupt",
            f"The stored {label} is unavailable.",
            status=500,
        ) from exc
    if not isinstance(value, dict):
        raise TruthAnalysisResearchError(
            "research_receipt_corrupt",
            f"The stored {label} is unavailable.",
            status=500,
        )
    return value


def _bound_run(
    run_id: str,
    agent_session_id: str | None,
    *,
    require_active: bool,
) -> TruthAnalysisRuntimeRun:
    run = truth_analysis_runtime.get_run(str(run_id or "").strip())
    if run is None:
        raise TruthAnalysisResearchError(
            "analysis_run_not_found",
            "The Truth analysis run is unavailable.",
            status=404,
        )
    if not agent_session_id or agent_session_id != run.session_id:
        raise TruthAnalysisResearchError(
            "analysis_session_forbidden",
            "This worker session is not authorized for that Truth analysis run.",
            status=403,
        )
    if require_active and run.status not in _ACTIVE_RUN_STATUSES:
        raise TruthAnalysisResearchError(
            "analysis_run_terminal",
            "This Truth analysis run can no longer perform external research.",
            status=409,
        )
    return run


def _operation(
    run_id: str,
    kind: Literal["search", "fetch"],
    operation_key: str,
) -> sqlite3.Row | None:
    with _connect() as conn:
        _ensure_schema(conn)
        return conn.execute(
            "SELECT * FROM cowork_truth_analysis_research_operations "
            "WHERE run_id = ? AND kind = ? AND operation_key = ?",
            (run_id, kind, operation_key),
        ).fetchone()


def _reserve_operation(
    run_id: str,
    kind: Literal["search", "fetch"],
    operation_key: str,
    *,
    maximum: int,
) -> tuple[sqlite3.Row, bool]:
    now = utc_now()
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM cowork_truth_analysis_research_operations "
            "WHERE run_id = ? AND kind = ? AND operation_key = ?",
            (run_id, kind, operation_key),
        ).fetchone()
        if existing is not None:
            if str(existing["state"]) == "pending":
                try:
                    updated = datetime.fromisoformat(str(existing["updated_at"]))
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - updated).total_seconds()
                except ValueError:
                    age = PENDING_OPERATION_TTL_SECONDS + 1
                if age > PENDING_OPERATION_TTL_SECONDS:
                    payload = json.dumps(
                        {
                            "error_code": "research_outcome_unknown",
                            "error": (
                                "A previous external research attempt ended before "
                                "its outcome was recorded. It will not be replayed."
                            ),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    conn.execute(
                        "UPDATE cowork_truth_analysis_research_operations "
                        "SET state = 'outcome_unknown', payload_json = ?, updated_at = ? "
                        "WHERE operation_id = ? AND state = 'pending'",
                        (payload, now, str(existing["operation_id"])),
                    )
                    existing = conn.execute(
                        "SELECT * FROM cowork_truth_analysis_research_operations "
                        "WHERE operation_id = ?",
                        (str(existing["operation_id"]),),
                    ).fetchone()
            conn.commit()
            assert existing is not None
            return existing, False
        operation_keys = {
            str(row[0])
            for row in conn.execute(
                "SELECT operation_key FROM cowork_truth_analysis_research_operations "
                "WHERE run_id = ? AND kind = ?",
                (run_id, kind),
            ).fetchall()
        }
        if kind == "search":
            durable_keys = {
                sha256_text(str(row[0]))
                for row in conn.execute(
                    "SELECT query FROM cowork_truth_analysis_searches WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
            }
        else:
            durable_keys = {
                str(row[0])
                for row in conn.execute(
                    "SELECT hit_id FROM cowork_truth_analysis_fetches WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
            }
        count = len(operation_keys | durable_keys)
        if count >= maximum:
            conn.rollback()
            raise TruthAnalysisResearchError(
                f"{kind}_limit_reached",
                (
                    "This Truth analysis run has reached its bounded web-query limit."
                    if kind == "search"
                    else "This Truth analysis run has reached its bounded fetch limit."
                ),
                status=409,
            )
        operation_id = new_id()
        conn.execute(
            "INSERT INTO cowork_truth_analysis_research_operations ("
            "operation_id, run_id, kind, operation_key, state, payload_json, "
            "receipt_id, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', "
            "'{}', '', ?, ?)",
            (operation_id, run_id, kind, operation_key, now, now),
        )
        row = conn.execute(
            "SELECT * FROM cowork_truth_analysis_research_operations "
            "WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        conn.commit()
    assert row is not None
    return row, True


def _prepare_operation(operation_id: str, payload: Mapping[str, Any]) -> None:
    serialized = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE cowork_truth_analysis_research_operations "
            "SET state = 'prepared', payload_json = ?, updated_at = ? "
            "WHERE operation_id = ? AND state = 'pending'",
            (serialized, utc_now(), operation_id),
        )


def _complete_operation(operation_id: str, receipt_id: str) -> None:
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE cowork_truth_analysis_research_operations "
            "SET state = 'completed', receipt_id = ?, updated_at = ? "
            "WHERE operation_id = ? AND state IN ('prepared', 'completed')",
            (receipt_id, utc_now(), operation_id),
        )


def _normalized_query(query: str) -> str:
    normalized = " ".join(str(query or "").split())
    if not normalized or len(normalized) > MAX_QUERY_CHARS:
        raise TruthAnalysisResearchError(
            "invalid_search_query",
            f"A web query must contain 1 through {MAX_QUERY_CHARS} characters.",
        )
    return normalized


def normalize_query(query: str) -> str:
    """Return the exact bounded query text supplied to the search provider."""

    return _normalized_query(query)


def _bounded(value: object, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


def _ip_is_public(value: IPv4Address | IPv6Address) -> bool:
    mapped = value.ipv4_mapped if isinstance(value, IPv6Address) else None
    if mapped is not None:
        return mapped.is_global and not (
            mapped.is_private
            or mapped.is_loopback
            or mapped.is_link_local
            or mapped.is_multicast
            or mapped.is_reserved
            or mapped.is_unspecified
        )
    return value.is_global and not (
        value.is_private
        or value.is_loopback
        or value.is_link_local
        or value.is_multicast
        or value.is_reserved
        or value.is_unspecified
    )


def _url_syntax(url: str) -> str:
    raw = str(url or "").strip()
    if (
        not raw
        or len(raw) > MAX_URL_CHARS
        or _CONTROL_OR_SPACE.search(raw)
        or "\\" in raw
    ):
        raise TruthAnalysisResearchError(
            "unsafe_destination", "The source URL is not a safe public HTTP URL."
        )
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise TruthAnalysisResearchError(
            "unsafe_destination", "The source URL is not a safe public HTTP URL."
        ) from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise TruthAnalysisResearchError(
            "unsafe_destination", "Only public HTTP and HTTPS sources may be fetched."
        )
    if port is not None and port < 1:
        raise TruthAnalysisResearchError(
            "unsafe_destination", "The source URL uses an invalid network port."
        )
    expected_port = 443 if scheme == "https" else 80
    effective_port = port or expected_port
    if effective_port != expected_port:
        raise TruthAnalysisResearchError(
            "unsafe_destination",
            "Only standard HTTP and HTTPS source ports may be fetched.",
        )
    if parsed.username is not None or parsed.password is not None:
        raise TruthAnalysisResearchError(
            "unsafe_destination", "Source URLs cannot contain credentials."
        )
    host = parsed.hostname.rstrip(".").lower()
    if not host or "%" in host:
        raise TruthAnalysisResearchError(
            "unsafe_destination", "The source URL is not a safe public HTTP URL."
        )
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise TruthAnalysisResearchError(
            "unsafe_destination", "The source hostname is invalid."
        ) from exc
    if ascii_host == "localhost" or ascii_host.endswith(_LOCAL_HOST_SUFFIXES):
        raise TruthAnalysisResearchError(
            "unsafe_destination", "Local-network source URLs cannot be fetched."
        )
    try:
        literal = ip_address(ascii_host)
    except ValueError:
        literal = None
    if literal is not None and not _ip_is_public(literal):
        raise TruthAnalysisResearchError(
            "unsafe_destination", "Private or non-public source URLs cannot be fetched."
        )
    rendered_host = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    netloc = rendered_host if port is None else f"{rendered_host}:{port}"
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="=&?/:;+,%@!$'()*-._~")
    return urlunsplit((scheme, netloc, path, query, ""))


def _default_resolver(host: str, port: int) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise TruthAnalysisResearchError(
            "destination_unresolvable",
            "The source hostname could not be resolved.",
            status=502,
            retryable=True,
        ) from exc
    return tuple(dict.fromkeys(str(item[4][0]) for item in records))


def _validated_destination(url: str, resolver: Resolver) -> _ValidatedDestination:
    normalized = _url_syntax(url)
    parsed = urlsplit(normalized)
    assert parsed.hostname is not None
    try:
        literal = ip_address(parsed.hostname)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = (str(literal),)
    else:
        try:
            addresses = tuple(
                resolver(
                    parsed.hostname,
                    (
                        parsed.port
                        if parsed.port is not None
                        else (443 if parsed.scheme == "https" else 80)
                    ),
                )
            )
        except TruthAnalysisResearchError as exc:
            exc.details.setdefault("external_egress", True)
            raise
    if not addresses:
        raise TruthAnalysisResearchError(
            "destination_unresolvable",
            "The source hostname did not resolve to a public address.",
            status=502,
            retryable=True,
        )
    for raw in addresses:
        try:
            address = ip_address(str(raw))
        except ValueError as exc:
            raise TruthAnalysisResearchError(
                "destination_unresolvable",
                "The source hostname returned an invalid address.",
                status=502,
            ) from exc
        if not _ip_is_public(address):
            raise TruthAnalysisResearchError(
                "unsafe_destination",
                "The source hostname resolves to a private or non-public network.",
                status=403,
                details={"external_egress": literal is None},
            )
    return _ValidatedDestination(normalized, tuple(str(item) for item in addresses))


def _connected_socket(
    *,
    address: str,
    port: int,
    timeout_s: float,
) -> socket.socket:
    parsed = ip_address(address)
    family = socket.AF_INET6 if isinstance(parsed, IPv6Address) else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        endpoint: tuple[Any, ...] = (
            (str(parsed), port, 0, 0)
            if family == socket.AF_INET6
            else (str(parsed), port)
        )
        sock.connect(endpoint)
    except Exception:
        sock.close()
        raise
    return sock


def _default_requester(
    url: str,
    addresses: Sequence[str],
    timeout_s: float,
    max_bytes: int,
) -> ResearchHttpResponse:
    """Issue one HTTP request on a socket pinned to a validated public IP.

    The original hostname remains the TLS SNI/certificate name and Host header;
    DNS is never consulted again after the broker validates ``addresses``.
    """

    parsed = urlsplit(url)
    assert parsed.hostname is not None
    port = (
        parsed.port
        if parsed.port is not None
        else (443 if parsed.scheme == "https" else 80)
    )
    deadline = time.monotonic() + timeout_s
    network_socket: socket.socket | None = None
    last_error: Exception | None = None
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            network_socket = _connected_socket(
                address=address,
                port=port,
                timeout_s=remaining,
            )
            break
        except OSError as exc:
            last_error = exc
    if network_socket is None:
        if last_error is not None:
            raise last_error
        raise TimeoutError("bounded source connection timed out")

    if parsed.scheme == "https":
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            network_socket.close()
            raise TimeoutError("bounded TLS connection timed out")
        network_socket.settimeout(remaining)
        context = ssl.create_default_context()
        try:
            network_socket = context.wrap_socket(
                network_socket,
                server_hostname=parsed.hostname,
            )
        except Exception:
            network_socket.close()
            raise

    headers = {
        "Accept": (
            "text/html, text/plain, application/xhtml+xml, application/json, "
            "application/xml;q=0.8"
        ),
        "Accept-Encoding": "identity",
        "Connection": "close",
        "User-Agent": "work-buddy-truth-research/1.0",
    }
    default_port = 443 if parsed.scheme == "https" else 80
    rendered_host = (
        f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    )
    headers["Host"] = (
        rendered_host if port == default_port else f"{rendered_host}:{port}"
    )
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout_s)
    connection.sock = network_socket
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("bounded source request timed out")
        network_socket.settimeout(remaining)
        connection.request("GET", target, headers=headers)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("bounded source response timed out")
        network_socket.settimeout(remaining)
        response = connection.getresponse()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        if response.status in _REDIRECT_STATUSES:
            return ResearchHttpResponse(response.status, response_headers, b"")
        length = response_headers.get("content-length")
        if length is not None:
            try:
                if int(length) > max_bytes:
                    raise TruthAnalysisResearchError(
                        "response_too_large",
                        "The source is larger than the bounded fetch limit.",
                        status=422,
                    )
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("bounded source read timed out")
            network_socket.settimeout(remaining)
            chunk = response.read(min(64 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise TruthAnalysisResearchError(
                    "response_too_large",
                    "The source is larger than the bounded fetch limit.",
                    status=422,
                )
            chunks.append(chunk)
        return ResearchHttpResponse(response.status, response_headers, b"".join(chunks))
    finally:
        connection.close()


def _decode_body(body: bytes, content_type: str) -> str:
    match = _CHARSET.search(content_type)
    encoding = "utf-8" if match is None else match.group(1)
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _extract_text(
    body: bytes,
    content_type: str,
    source_url: str,
) -> tuple[str, str, str, int, int, str, bool]:
    media_type = (content_type.split(";", 1)[0].strip().lower() or "text/plain")
    textual = (
        media_type.startswith("text/")
        or media_type in {"application/json", "application/xml", "application/xhtml+xml"}
        or media_type.endswith("+json")
        or media_type.endswith("+xml")
    )
    if not textual:
        raise TruthAnalysisResearchError(
            "unsupported_content_type",
            "The admitted source did not return supported textual content.",
            status=422,
        )
    decoded = _decode_body(body, content_type)
    extractor = "response_text"
    text = decoded
    if media_type in {"text/html", "application/xhtml+xml"}:
        try:
            import trafilatura

            extracted = trafilatura.extract(
                decoded,
                url=source_url,
                favor_recall=True,
                include_comments=False,
            )
        except Exception as exc:  # noqa: BLE001 - parser failures are typed below
            raise TruthAnalysisResearchError(
                "text_extraction_failed",
                "Readable text could not be extracted from the admitted source.",
                status=422,
            ) from exc
        if extracted:
            text = extracted
            extractor = "trafilatura"
    if not text.strip():
        raise TruthAnalysisResearchError(
            "empty_source",
            "The admitted source did not contain readable text.",
            status=422,
        )
    full_bytes = text.encode("utf-8")
    full_sha256 = sha256_text(text)
    truncated = len(full_bytes) > MAX_CAPTURED_TEXT_BYTES
    if truncated:
        text = full_bytes[:MAX_CAPTURED_TEXT_BYTES].decode(
            "utf-8", errors="ignore"
        )
    captured_bytes = len(text.encode("utf-8"))
    return (
        text,
        media_type,
        extractor,
        len(full_bytes),
        captured_bytes,
        full_sha256,
        truncated,
    )


def _fetch_public_text(
    url: str,
    *,
    resolver: Resolver,
    requester: Requester,
) -> _FetchedPage:
    requested_url = _url_syntax(url)
    current = requested_url
    chain: list[str] = []
    visited: set[str] = set()
    deadline = time.monotonic() + MAX_TOTAL_FETCH_SECONDS
    redirects = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TruthAnalysisResearchError(
                "fetch_timeout",
                "The admitted source did not respond within the bounded fetch time.",
                status=504,
                retryable=True,
                details={"external_egress": bool(chain)},
            )
        try:
            destination = _validated_destination(current, resolver)
            current = destination.url
        except TruthAnalysisResearchError as exc:
            exc.details.setdefault("external_egress", bool(chain))
            raise
        if current in visited:
            raise TruthAnalysisResearchError(
                "redirect_loop",
                "The admitted source redirected in a loop.",
                status=422,
                details={"external_egress": bool(chain)},
            )
        visited.add(current)
        chain.append(current)
        try:
            response = requester(
                current,
                destination.addresses,
                min(MAX_REQUEST_SECONDS, remaining),
                MAX_RESPONSE_BYTES,
            )
        except TruthAnalysisResearchError as exc:
            exc.details.setdefault("external_egress", True)
            raise
        except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException) as exc:
            raise TruthAnalysisResearchError(
                "fetch_unavailable",
                "The admitted source could not be fetched.",
                status=502,
                retryable=True,
                details={"external_egress": True},
            ) from exc
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise TruthAnalysisResearchError(
                "response_too_large",
                "The source is larger than the bounded fetch limit.",
                status=422,
                details={"external_egress": True},
            )
        headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
        if response.status_code in _REDIRECT_STATUSES:
            location = headers.get("location", "").strip()
            if not location:
                raise TruthAnalysisResearchError(
                    "invalid_redirect",
                    "The admitted source returned an invalid redirect.",
                    status=422,
                    details={"external_egress": True},
                )
            if redirects >= MAX_REDIRECTS:
                raise TruthAnalysisResearchError(
                    "too_many_redirects",
                    "The admitted source exceeded the bounded redirect limit.",
                    status=422,
                    details={"external_egress": True},
                )
            redirects += 1
            current = urljoin(current, location)
            continue
        if response.status_code < 200 or response.status_code >= 300:
            raise TruthAnalysisResearchError(
                "fetch_http_error",
                f"The admitted source returned HTTP {response.status_code}.",
                status=422,
                details={"external_egress": True},
            )
        content_encoding = headers.get("content-encoding", "identity").strip().lower()
        if content_encoding not in {"", "identity"}:
            raise TruthAnalysisResearchError(
                "unsupported_content_encoding",
                "The admitted source ignored the bounded identity-encoding request.",
                status=422,
                details={"external_egress": True},
            )
        content_type = headers.get("content-type", "text/plain")
        (
            text,
            media_type,
            extractor,
            extracted_text_bytes,
            captured_text_bytes,
            full_text_sha256,
            text_truncated,
        ) = _extract_text(response.body, content_type, current)
        return _FetchedPage(
            requested_url=requested_url,
            source_url=current,
            text=text,
            media_type=media_type,
            http_status=response.status_code,
            extractor=extractor,
            redirect_chain=tuple(chain),
            bytes_received=len(response.body),
            extracted_text_bytes=extracted_text_bytes,
            captured_text_bytes=captured_text_bytes,
            full_text_sha256=full_text_sha256,
            text_truncated=text_truncated,
        )


def _search_payload(
    *,
    query: str,
    status: Literal["completed", "failed"],
    hits: Sequence[ResearchSearchLead],
    external_egress: bool,
    error_code: str = "",
    error: str = "",
    searched_at: str | None = None,
) -> dict[str, Any]:
    return {
        "query": query,
        "status": status,
        "hits": [item.to_dict() for item in hits],
        "external_egress": external_egress,
        "error_code": error_code,
        "error": error,
        "searched_at": searched_at or utc_now(),
    }


def _sanitize_hits(results: Sequence[SearchHit]) -> tuple[ResearchSearchLead, ...]:
    admitted: list[ResearchSearchLead] = []
    seen_urls: set[str] = set()
    for item in results:
        if len(admitted) >= MAX_HITS_PER_QUERY:
            break
        if not isinstance(item, SearchHit):
            continue
        try:
            url = _url_syntax(item.url)
        except TruthAnalysisResearchError:
            continue
        if url in seen_urls:
            continue
        title = _bounded(item.title, 500)
        if not title:
            continue
        score = item.score
        normalized_score = (
            float(score)
            if isinstance(score, (int, float))
            and not isinstance(score, bool)
            and math.isfinite(float(score))
            else None
        )
        seen_urls.add(url)
        admitted.append(
            ResearchSearchLead(
                hit_id=new_id(),
                title=title,
                url=url,
                snippet=_bounded(item.snippet, 2_000),
                provider=_bounded(item.provider, 80) or "unknown",
                published=(None if item.published is None else _bounded(item.published, 100)),
                score=normalized_score,
                rank=len(admitted) + 1,
            )
        )
    return tuple(admitted)


def _search_from_receipt(
    run: TruthAnalysisRuntimeRun,
    receipt: truth_analysis_runtime.TruthAnalysisSearchReceipt,
    *,
    replayed: bool,
) -> ResearchSearchResult:
    operation = _operation(run.run_id, "search", sha256_text(receipt.query))
    payload = (
        _json_object(str(operation["payload_json"]), "search receipt")
        if operation is not None and str(operation["payload_json"]) != "{}"
        else {}
    )
    return ResearchSearchResult(
        search_id=receipt.search_id,
        run_id=run.run_id,
        query=receipt.query,
        status="completed" if receipt.status == "completed" else "failed",
        hits=tuple(ResearchSearchLead.from_mapping(item) for item in receipt.hits),
        external_egress=receipt.external_egress,
        error_code=str(payload.get("error_code") or ""),
        error=receipt.error,
        searched_at=receipt.searched_at,
        replayed=replayed,
    )


def _record_search_payload(
    run: TruthAnalysisRuntimeRun,
    operation_id: str,
    payload: Mapping[str, Any],
    *,
    replayed: bool,
) -> ResearchSearchResult:
    hits = [dict(item) for item in payload.get("hits", []) if isinstance(item, Mapping)]
    receipt, runtime_replayed = truth_analysis_runtime.record_search_receipt(
        run_id=run.run_id,
        query=str(payload["query"]),
        status=str(payload["status"]),
        hits=hits,
        external_egress=bool(payload.get("external_egress")),
        error=str(payload.get("error") or ""),
        max_searches=MAX_QUERIES_PER_RUN,
        at=str(payload["searched_at"]),
    )
    _complete_operation(operation_id, receipt.search_id)
    return _search_from_receipt(
        run,
        receipt,
        replayed=replayed or runtime_replayed,
    )


def search(
    *,
    run_id: str,
    query: str,
    agent_session_id: str | None,
    searcher: SearchCallable | None = None,
) -> ResearchSearchResult:
    """Search at most three distinct queries for one bound worker run.

    Replaying the same normalized query returns the original admitted leads and
    never calls the provider again.  Provider snippets remain explicitly
    lead-only and raw provider page text is discarded.
    """

    run = _bound_run(run_id, agent_session_id, require_active=True)
    normalized = _normalized_query(query)
    for receipt in truth_analysis_runtime.search_receipts_for_run(run.run_id):
        if receipt.query == normalized:
            return _search_from_receipt(run, receipt, replayed=True)
    key = sha256_text(normalized)
    operation, reserved = _reserve_operation(
        run.run_id,
        "search",
        key,
        maximum=MAX_QUERIES_PER_RUN,
    )
    state = str(operation["state"])
    if state in {"prepared", "completed"}:
        payload = _json_object(str(operation["payload_json"]), "search receipt")
        return _record_search_payload(
            run,
            str(operation["operation_id"]),
            payload,
            replayed=True,
        )
    if state == "outcome_unknown":
        payload = _json_object(str(operation["payload_json"]), "research outcome")
        raise TruthAnalysisResearchError(
            "research_outcome_unknown",
            str(payload.get("error") or "The prior research outcome is unknown."),
            status=409,
            retryable=False,
        )
    if not reserved:
        raise TruthAnalysisResearchError(
            "search_in_progress",
            "This bounded web query is already in progress.",
            status=409,
            retryable=True,
        )
    if searcher is None:
        from work_buddy.websearch import router

        searcher = router.search
    try:
        raw_results = searcher(
            normalized,
            max_results=MAX_HITS_PER_QUERY,
            cache=False,
        )
        hits = _sanitize_hits(raw_results)
        payload = _search_payload(
            query=normalized,
            status="completed",
            hits=hits,
            external_egress=True,
        )
    except Exception as exc:  # provider types are projected without leaking details
        from work_buddy.websearch.errors import WebSearchError, WebSearchProviderDisabled

        if not isinstance(exc, WebSearchError):
            error_code = "websearch_unavailable"
            external_egress = True
        else:
            error_code = exc.error_kind
            external_egress = not isinstance(exc, WebSearchProviderDisabled)
        payload = _search_payload(
            query=normalized,
            status="failed",
            hits=(),
            external_egress=external_egress,
            error_code=error_code,
            error="Web search was unavailable for this bounded analysis.",
        )
    _prepare_operation(str(operation["operation_id"]), payload)
    return _record_search_payload(
        run,
        str(operation["operation_id"]),
        payload,
        replayed=False,
    )


def _fetch_payload(
    *,
    hit: Mapping[str, Any],
    status: Literal["completed", "unavailable", "failed"],
    page: _FetchedPage | None,
    external_egress: bool,
    error_code: str = "",
    error: str = "",
) -> dict[str, Any]:
    acquired_at = utc_now()
    requested_url = str(hit.get("url") or "")
    title = str(hit.get("title") or "")
    metadata: dict[str, Any] = {}
    if page is not None:
        metadata = {
            "schema": "wb.cowork.truth-analysis-web-acquisition/v1",
            "method": "guarded_direct_http_get",
            "requested_url": page.requested_url,
            "source_url": page.source_url,
            "redirect_chain": list(page.redirect_chain),
            "http_status": page.http_status,
            "media_type": page.media_type,
            "bytes_received": page.bytes_received,
            "extracted_text_bytes": page.extracted_text_bytes,
            "captured_text_bytes": page.captured_text_bytes,
            "captured_text_sha256": sha256_text(page.text),
            "full_extracted_text_sha256": page.full_text_sha256,
            "text_truncated": page.text_truncated,
            "extractor": page.extractor,
            "search_provider": str(hit.get("provider") or ""),
            "limits": {
                "max_redirects": MAX_REDIRECTS,
                "max_response_bytes": MAX_RESPONSE_BYTES,
                "max_captured_text_bytes": MAX_CAPTURED_TEXT_BYTES,
                "max_total_seconds": MAX_TOTAL_FETCH_SECONDS,
            },
        }
    return {
        "hit_id": str(hit.get("hit_id") or ""),
        "status": status,
        "requested_url": requested_url if page is None else page.requested_url,
        "source_url": requested_url if page is None else page.source_url,
        "title": title,
        "exact_text": "" if page is None else page.text,
        "text_sha256": "" if page is None else sha256_text(page.text),
        "media_type": "" if page is None else page.media_type,
        "http_status": None if page is None else page.http_status,
        "extractor": "none" if page is None else page.extractor,
        "redirect_chain": [] if page is None else list(page.redirect_chain),
        "acquisition_metadata": metadata,
        "external_egress": external_egress,
        "error_code": error_code,
        "error": error,
        "acquired_at": acquired_at,
    }


def _persist_acquisition(fetch_id: str, run_id: str, payload: Mapping[str, Any]) -> None:
    if payload.get("status") != "completed":
        return
    redirects = json.dumps(
        list(payload.get("redirect_chain", [])),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    metadata = json.dumps(
        dict(payload.get("acquisition_metadata", {})),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    values = (
        fetch_id,
        run_id,
        str(payload["hit_id"]),
        str(payload["requested_url"]),
        str(payload["source_url"]),
        str(payload["title"]),
        str(payload["exact_text"]),
        str(payload["text_sha256"]),
        str(payload["media_type"]),
        int(payload["http_status"]),
        str(payload["extractor"]),
        redirects,
        metadata,
        str(payload["acquired_at"]),
    )
    with _connect() as conn:
        _ensure_schema(conn)
        existing = conn.execute(
            "SELECT * FROM cowork_truth_analysis_fetch_acquisitions "
            "WHERE run_id = ? AND hit_id = ?",
            (run_id, str(payload["hit_id"])),
        ).fetchone()
        if existing is not None:
            comparable = (
                str(existing["fetch_id"]),
                str(existing["run_id"]),
                str(existing["hit_id"]),
                str(existing["requested_url"]),
                str(existing["source_url"]),
                str(existing["title"]),
                str(existing["exact_text"]),
                str(existing["text_sha256"]),
                str(existing["media_type"]),
                int(existing["http_status"]),
                str(existing["extractor"]),
                str(existing["redirect_chain_json"]),
                str(existing["acquisition_json"]),
                str(existing["acquired_at"]),
            )
            if comparable != values:
                raise TruthAnalysisResearchError(
                    "fetch_replay_changed",
                    "The stored source acquisition changed during replay.",
                    status=409,
                )
            return
        conn.execute(
            "INSERT INTO cowork_truth_analysis_fetch_acquisitions ("
            "fetch_id, run_id, hit_id, requested_url, source_url, title, "
            "exact_text, text_sha256, media_type, http_status, extractor, "
            "redirect_chain_json, acquisition_json, acquired_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )


def _acquisition(fetch_id: str, run_id: str) -> sqlite3.Row | None:
    with _connect() as conn:
        _ensure_schema(conn)
        return conn.execute(
            "SELECT * FROM cowork_truth_analysis_fetch_acquisitions "
            "WHERE fetch_id = ? AND run_id = ?",
            (fetch_id, run_id),
        ).fetchone()


def _fetch_from_runtime(
    run: TruthAnalysisRuntimeRun,
    receipt: truth_analysis_runtime.TruthAnalysisFetchReceipt,
) -> ResearchFetchReceipt:
    row = _acquisition(receipt.fetch_id, run.run_id)
    operation = _operation(run.run_id, "fetch", receipt.hit_id)
    payload = (
        _json_object(str(operation["payload_json"]), "fetch receipt")
        if operation is not None and str(operation["payload_json"]) != "{}"
        else {}
    )
    if row is None:
        return ResearchFetchReceipt(
            fetch_id=receipt.fetch_id,
            run_id=run.run_id,
            hit_id=receipt.hit_id,
            status=receipt.status,  # type: ignore[arg-type]
            requested_url=receipt.url,
            source_url=receipt.canonical_url,
            title=receipt.title,
            exact_text=receipt.text,
            text_sha256=receipt.content_sha256,
            media_type=str(payload.get("media_type") or ""),
            http_status=(
                int(payload["http_status"])
                if isinstance(payload.get("http_status"), int)
                else None
            ),
            extractor=receipt.extractor,
            redirect_chain=tuple(
                str(item) for item in payload.get("redirect_chain", [])
            ),
            acquisition_metadata=dict(payload.get("acquisition_metadata", {})),
            external_egress=receipt.external_egress,
            error_code=str(payload.get("error_code") or ""),
            error=receipt.error,
            acquired_at=receipt.fetched_at,
        )
    redirects = json.loads(str(row["redirect_chain_json"]))
    metadata = _json_object(str(row["acquisition_json"]), "acquisition metadata")
    return ResearchFetchReceipt(
        fetch_id=receipt.fetch_id,
        run_id=run.run_id,
        hit_id=receipt.hit_id,
        status=receipt.status,  # type: ignore[arg-type]
        requested_url=str(row["requested_url"]),
        source_url=str(row["source_url"]),
        title=str(row["title"]),
        exact_text=str(row["exact_text"]),
        text_sha256=str(row["text_sha256"]),
        media_type=str(row["media_type"]),
        http_status=int(row["http_status"]),
        extractor=str(row["extractor"]),
        redirect_chain=tuple(str(item) for item in redirects),
        acquisition_metadata=metadata,
        external_egress=receipt.external_egress,
        error_code=str(payload.get("error_code") or ""),
        error=receipt.error,
        acquired_at=str(row["acquired_at"]),
    )


def _record_fetch_payload(
    run: TruthAnalysisRuntimeRun,
    operation_id: str,
    payload: Mapping[str, Any],
    *,
    replayed: bool,
) -> ResearchFetchResult:
    fetch_id = sha256_text(
        canonical_json(
            {
                "domain": "work-buddy.cowork-truth-fetch/v1",
                "run_id": run.run_id,
                "hit_id": str(payload["hit_id"]),
            }
        )
    )[:32]
    _persist_acquisition(fetch_id, run.run_id, payload)
    receipt, runtime_replayed = truth_analysis_runtime.record_fetch_receipt(
        run_id=run.run_id,
        hit_id=str(payload["hit_id"]),
        status=str(payload["status"]),
        url=str(payload["requested_url"]),
        canonical_url=str(payload["source_url"]),
        title=str(payload["title"]),
        text=str(payload["exact_text"]),
        content_sha256=str(payload["text_sha256"]),
        extractor=str(payload["extractor"]),
        external_egress=bool(payload.get("external_egress")),
        error=str(payload.get("error") or ""),
        max_fetches=MAX_FETCHES_PER_RUN,
        at=str(payload["acquired_at"]),
    )
    _complete_operation(operation_id, receipt.fetch_id)
    return ResearchFetchResult(
        receipt=_fetch_from_runtime(run, receipt),
        replayed=replayed or runtime_replayed,
    )


def fetch(
    *,
    run_id: str,
    hit_id: str,
    agent_session_id: str | None,
    resolver: Resolver | None = None,
    requester: Requester | None = None,
) -> ResearchFetchResult:
    """Fetch one admitted lead through the guarded public-network boundary."""

    run = _bound_run(run_id, agent_session_id, require_active=True)
    normalized_hit_id = str(hit_id or "").strip()
    for receipt in truth_analysis_runtime.fetch_receipts_for_run(run.run_id):
        if receipt.hit_id == normalized_hit_id:
            return ResearchFetchResult(
                receipt=_fetch_from_runtime(run, receipt), replayed=True
            )
    hit = truth_analysis_runtime.search_hit_for_run(run.run_id, normalized_hit_id)
    if hit is None:
        raise TruthAnalysisResearchError(
            "search_hit_not_admitted",
            "Only an exact search hit admitted by this run may be fetched.",
            status=403,
        )
    operation, reserved = _reserve_operation(
        run.run_id,
        "fetch",
        normalized_hit_id,
        maximum=MAX_FETCHES_PER_RUN,
    )
    state = str(operation["state"])
    if state in {"prepared", "completed"}:
        payload = _json_object(str(operation["payload_json"]), "fetch receipt")
        return _record_fetch_payload(
            run,
            str(operation["operation_id"]),
            payload,
            replayed=True,
        )
    if state == "outcome_unknown":
        payload = _json_object(str(operation["payload_json"]), "research outcome")
        raise TruthAnalysisResearchError(
            "research_outcome_unknown",
            str(payload.get("error") or "The prior research outcome is unknown."),
            status=409,
            retryable=False,
        )
    if not reserved:
        raise TruthAnalysisResearchError(
            "fetch_in_progress",
            "This admitted source fetch is already in progress.",
            status=409,
            retryable=True,
        )
    active_resolver = resolver or _default_resolver
    active_requester = requester or _default_requester
    try:
        page = _fetch_public_text(
            str(hit.get("url") or ""),
            resolver=active_resolver,
            requester=active_requester,
        )
        payload = _fetch_payload(
            hit=hit,
            status="completed",
            page=page,
            external_egress=True,
        )
    except TruthAnalysisResearchError as exc:
        payload = _fetch_payload(
            hit=hit,
            status="failed",
            page=None,
            external_egress=bool(exc.details.get("external_egress")),
            error_code=exc.code,
            error=str(exc),
        )
    _prepare_operation(str(operation["operation_id"]), payload)
    return _record_fetch_payload(
        run,
        str(operation["operation_id"]),
        payload,
        replayed=False,
    )


def get_receipt(
    *,
    run_id: str,
    fetch_id: str,
    agent_session_id: str | None,
) -> ResearchFetchReceipt | None:
    """Look up one exact run-owned acquisition, including after run completion."""

    run = _bound_run(run_id, agent_session_id, require_active=False)
    receipt = truth_analysis_runtime.get_fetch_receipt(run.run_id, fetch_id)
    return None if receipt is None else _fetch_from_runtime(run, receipt)


def receipts_for_run(
    *,
    run_id: str,
    agent_session_id: str | None,
) -> tuple[ResearchFetchReceipt, ...]:
    run = _bound_run(run_id, agent_session_id, require_active=False)
    return tuple(
        _fetch_from_runtime(run, item)
        for item in truth_analysis_runtime.fetch_receipts_for_run(run.run_id)
    )


def _source_coverage(run: TruthAnalysisRuntimeRun) -> list[dict[str, Any]]:
    base = [
        dict(item)
        for item in run.request.get("source_coverage", [])
        if isinstance(item, Mapping)
    ]
    searches = truth_analysis_runtime.search_receipts_for_run(run.run_id)
    if not searches:
        return base
    fetches = truth_analysis_runtime.fetch_receipts_for_run(run.run_id)
    completed_searches = sum(item.status == "completed" for item in searches)
    hit_count = sum(len(item.hits) for item in searches if item.status == "completed")
    completed_fetches = sum(item.status == "completed" for item in fetches)
    failed_searches = len(searches) - completed_searches
    web = {
        "source": "web",
        "status": "searched" if completed_searches else "failed",
        "detail": (
            f"{completed_searches} bounded queries returned {hit_count} leads; "
            f"{completed_fetches} admitted sources supplied fetched text; "
            f"{failed_searches} queries failed."
        ),
        "external_egress": any(item.external_egress for item in searches)
        or any(item.external_egress for item in fetches),
    }
    return [web if item.get("source") == "web" else item for item in base]


def search_web(
    *,
    run_id: str,
    query: str,
    agent_session_id: str | None,
) -> dict[str, Any]:
    """Drop-in operation projection for the current Truth-analysis worker."""

    result = search(
        run_id=run_id,
        query=query,
        agent_session_id=agent_session_id,
    )
    run = _bound_run(run_id, agent_session_id, require_active=False)
    value = result.to_dict()
    return {
        "ok": result.status == "completed",
        "analysis_run_id": run.run_id,
        **{key: item for key, item in value.items() if key != "run_id"},
        "source_coverage": _source_coverage(run),
    }


def fetch_search_hit(
    *,
    run_id: str,
    hit_id: str,
    agent_session_id: str | None,
) -> dict[str, Any]:
    """Drop-in operation projection that accepts only an admitted hit ID."""

    result = fetch(
        run_id=run_id,
        hit_id=hit_id,
        agent_session_id=agent_session_id,
    )
    run = _bound_run(run_id, agent_session_id, require_active=False)
    receipt = result.receipt
    return {
        "ok": receipt.status == "completed",
        "analysis_run_id": run.run_id,
        "fetch_id": receipt.fetch_id,
        "hit_id": receipt.hit_id,
        "status": receipt.status,
        "title": receipt.title,
        "url": receipt.requested_url,
        "canonical_url": receipt.source_url,
        "text": receipt.exact_text,
        "content_sha256": receipt.text_sha256,
        "extractor": receipt.extractor,
        "media_type": receipt.media_type or None,
        "http_status": receipt.http_status,
        "redirect_chain": list(receipt.redirect_chain),
        "acquisition_metadata": dict(receipt.acquisition_metadata),
        "error_code": receipt.error_code or None,
        "error": receipt.error or None,
        "replayed": result.replayed,
        "source_coverage": _source_coverage(run),
    }


__all__ = [
    "MAX_CAPTURED_TEXT_BYTES",
    "MAX_FETCHES_PER_RUN",
    "MAX_HITS_PER_QUERY",
    "MAX_QUERIES_PER_RUN",
    "ResearchFetchReceipt",
    "ResearchFetchResult",
    "ResearchHttpResponse",
    "ResearchSearchLead",
    "ResearchSearchResult",
    "TruthAnalysisResearchError",
    "fetch",
    "fetch_search_hit",
    "get_receipt",
    "normalize_query",
    "receipts_for_run",
    "search",
    "search_web",
]
