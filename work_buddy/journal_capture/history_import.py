"""Pure, inert parsing primitives for the private legacy Journal import.

Markdown is treated as untrusted bytes.  This module never executes template,
Dataview, Datacore, JavaScript, command, or plugin syntax.  It inventories the
frozen corpus and assigns every byte to one deterministic disposition; the
private orchestration that selects a user-specific source root and mapping lives under
the ignored migration workspace rather than becoming a public capability.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PARSER_VERSION = "wb.legacy-journal-parser/v1"
INVENTORY_SCHEMA = "wb.legacy-journal-inventory/v1"
PARSE_SCHEMA = "wb.legacy-journal-parse/v1"
_DATE_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*(?:\r?\n|\r|$)", re.MULTILINE)
_OPEN_MARKER = re.compile(
    r"<!--\s*wb:journal-entry/v1\s+id=([^\s>]+)\s+"
    r"content-sha256=([0-9a-f]{64})\s*-->"
)
_CLOSE_MARKER = re.compile(r"<!--\s*/wb:journal-entry/v1\s+id=([^\s>]+)\s*-->")


class LegacyJournalImportError(RuntimeError):
    """Frozen input or parser invariants were violated."""


@dataclass(frozen=True, slots=True)
class LegacyJournalInventoryEntry:
    relative_path: str
    local_date: str | None
    byte_length: int
    mtime_ns: int
    raw_sha256: str
    encoding: str
    newline: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "relativePath": self.relative_path,
            "localDate": self.local_date,
            "byteLength": self.byte_length,
            "mtimeNs": self.mtime_ns,
            "rawSha256": self.raw_sha256,
            "encoding": self.encoding,
            "newline": self.newline,
        }


@dataclass(frozen=True, slots=True)
class LegacyManagedProjection:
    entry_id: str
    declared_content_sha256: str
    closing_marker_present: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "entryId": self.entry_id,
            "declaredContentSha256": self.declared_content_sha256,
            "closingMarkerPresent": self.closing_marker_present,
        }


@dataclass(frozen=True, slots=True)
class LegacyJournalSpan:
    logical_id: str
    disposition: str
    section_key: str | None
    start_byte: int
    end_byte: int
    raw_sha256: str
    normalized_sha256: str | None
    structural_sha256: str | None
    content: bytes = field(repr=False, compare=False)
    managed_projections: tuple[LegacyManagedProjection, ...] = ()
    reason_code: str | None = None

    def to_receipt(self) -> dict[str, Any]:
        """Return private metadata only; exact prose stays in Sources."""

        return {
            "logicalId": self.logical_id,
            "disposition": self.disposition,
            "sectionKey": self.section_key,
            "startByte": self.start_byte,
            "endByte": self.end_byte,
            "rawSha256": self.raw_sha256,
            "normalizedSha256": self.normalized_sha256,
            "structuralSha256": self.structural_sha256,
            "managedProjections": [item.to_dict() for item in self.managed_projections],
            "reasonCode": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class ParsedLegacyJournalDay:
    inventory: LegacyJournalInventoryEntry
    cohort_id: str
    spans: tuple[LegacyJournalSpan, ...]
    parse_sha256: str
    parser_version: str = PARSER_VERSION

    @property
    def quarantined(self) -> bool:
        return any(span.reason_code is not None for span in self.spans)

    def to_receipt(self) -> dict[str, Any]:
        return {
            "schema": PARSE_SCHEMA,
            "parserVersion": self.parser_version,
            "cohortId": self.cohort_id,
            "inventory": self.inventory.to_dict(),
            "spans": [span.to_receipt() for span in self.spans],
            "parseSha256": self.parse_sha256,
            "quarantined": self.quarantined,
        }


_SECTION_DISPOSITIONS: dict[str, tuple[str, str]] = {
    "sign-in": ("check_in_section", "check_in"),
    "tasks & objectives": ("planning_section", "planning"),
    "most important tasks (mits)": ("planning_section", "planning_mits"),
    "woop": ("planning_section", "planning_woop"),
    "hard thing first (htf-1)": ("planning_section", "planning_htf"),
    "irreversible micro-decision(s)": ("planning_section", "planning_decisions"),
    "calendar": ("generated_projection_section", "calendar"),
    "log": ("log_section", "day_stream"),
    "running notes / considerations": ("running_notes_section", "notes"),
    "sign-off": ("sign_off_section", "sign_off"),
    "reflection": ("sign_off_section", "reflection"),
    "aar - after-action review": ("sign_off_section", "aar"),
    "upcoming": ("sign_off_section", "upcoming"),
    "review trackers": ("sign_off_section", "review_trackers"),
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_json(value: Any) -> str:
    return _sha_bytes(_canonical_json(value).encode("utf-8"))


def _newline_kind(raw: bytes) -> str:
    crlf = raw.count(b"\r\n")
    lone_lf = raw.count(b"\n") - crlf
    lone_cr = raw.count(b"\r") - crlf
    present = sum(bool(value) for value in (crlf, lone_lf, lone_cr))
    if present > 1:
        return "mixed"
    if crlf:
        return "crlf"
    if lone_lf:
        return "lf"
    if lone_cr:
        return "cr"
    return "none"


def _local_date(relative_path: str) -> str | None:
    name = Path(relative_path).name
    match = _DATE_FILE.fullmatch(name)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None


def inventory_entry(path: Path, *, root: Path) -> LegacyJournalInventoryEntry:
    resolved_root = root.expanduser().resolve()
    resolved = path.expanduser().resolve()
    try:
        relative = resolved.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise LegacyJournalImportError("Journal input escaped its allowlisted root") from exc
    if path.is_symlink() or not resolved.is_file():
        raise LegacyJournalImportError("Journal input must be a regular, non-symlink file")
    raw = resolved.read_bytes()
    try:
        raw.decode("utf-8-sig")
        encoding = "utf-8-bom" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    except UnicodeDecodeError:
        encoding = "invalid"
    stat = resolved.stat()
    return LegacyJournalInventoryEntry(
        relative_path=relative,
        local_date=_local_date(relative),
        byte_length=len(raw),
        mtime_ns=int(stat.st_mtime_ns),
        raw_sha256=_sha_bytes(raw),
        encoding=encoding,
        newline=_newline_kind(raw),
    )


def freeze_inventory(
    root: str | Path,
    *,
    allowlist: Sequence[str] | None = None,
) -> tuple[LegacyJournalInventoryEntry, ...]:
    """Freeze exact direct-child Markdown inputs without reading their prose out."""

    resolved_root = Path(root).expanduser().resolve()
    if not resolved_root.is_dir():
        raise LegacyJournalImportError("Journal source root is unavailable")
    if allowlist is None:
        paths = sorted(resolved_root.glob("*.md"), key=lambda item: item.name)
    else:
        paths = []
        for relative in sorted(dict.fromkeys(allowlist)):
            candidate = Path(relative)
            if candidate.name != relative or candidate.suffix.lower() != ".md":
                raise LegacyJournalImportError("Journal allowlist entries must be file names")
            paths.append(resolved_root / candidate)
    return tuple(inventory_entry(path, root=resolved_root) for path in paths)


def inventory_sha256(entries: Iterable[LegacyJournalInventoryEntry]) -> str:
    return _sha_json(
        {
            "schema": INVENTORY_SCHEMA,
            "parserVersion": PARSER_VERSION,
            "files": [entry.to_dict() for entry in entries],
        }
    )


def verify_frozen_inventory(
    root: str | Path,
    expected: Sequence[LegacyJournalInventoryEntry],
) -> tuple[LegacyJournalInventoryEntry, ...]:
    """Re-hash the exact allowlist and reject additions, removals, or drift."""

    allowlist = [item.relative_path for item in expected]
    actual = freeze_inventory(root, allowlist=allowlist)
    if tuple(item.to_dict() for item in actual) != tuple(
        item.to_dict() for item in expected
    ):
        raise LegacyJournalImportError("The frozen Journal corpus changed")
    all_names = {path.name for path in Path(root).expanduser().resolve().glob("*.md")}
    if all_names != set(allowlist):
        raise LegacyJournalImportError("The Journal source file set changed")
    return actual


def _normalize_heading(value: str) -> str:
    normalized = value.strip().strip("*_`").strip()
    normalized = normalized.replace("—", "-").replace("–", "-")
    normalized = re.sub(r"\s+", " ", normalized).rstrip(":").casefold()
    if normalized.startswith("recon ("):
        return "recon"
    return normalized


def _section_contract(label: str) -> tuple[str, str] | None:
    normalized = _normalize_heading(label)
    if normalized == "recon":
        return "generated_projection_section", "recon"
    return _SECTION_DISPOSITIONS.get(normalized)


def _normalized_digest(content: bytes) -> str | None:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return _sha_bytes(normalized.encode("utf-8"))


def _structural_digest(content: bytes) -> str | None:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    normalized = re.sub(r"(?m)^([ \t]*)[+*](?=\s)", r"\1-", normalized)
    return _sha_bytes(normalized.encode("utf-8"))


def _managed_projections(content: bytes) -> tuple[LegacyManagedProjection, ...]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ()
    closes = {match.group(1) for match in _CLOSE_MARKER.finditer(text)}
    return tuple(
        LegacyManagedProjection(
            entry_id=match.group(1),
            declared_content_sha256=match.group(2),
            closing_marker_present=match.group(1) in closes,
        )
        for match in _OPEN_MARKER.finditer(text)
    )


def _logical_id(
    *,
    cohort_id: str,
    inventory: LegacyJournalInventoryEntry,
    disposition: str,
    section_key: str | None,
    start: int,
    end: int,
) -> str:
    # Parser version is intentionally absent. A safer parser may change the
    # receipt without minting another logical occurrence for the same span.
    value = {
        "schema": "wb.legacy-journal-logical-id/v1",
        "cohortId": cohort_id,
        "relativePath": inventory.relative_path,
        "fileSha256": inventory.raw_sha256,
        "disposition": disposition,
        "sectionKey": section_key,
        "startByte": start,
        "endByte": end,
    }
    return "jli_" + _sha_json(value)[:32]


def _span(
    raw: bytes,
    *,
    cohort_id: str,
    inventory: LegacyJournalInventoryEntry,
    disposition: str,
    section_key: str | None,
    start: int,
    end: int,
    reason_code: str | None = None,
) -> LegacyJournalSpan:
    content = raw[start:end]
    return LegacyJournalSpan(
        logical_id=_logical_id(
            cohort_id=cohort_id,
            inventory=inventory,
            disposition=disposition,
            section_key=section_key,
            start=start,
            end=end,
        ),
        disposition=disposition,
        section_key=section_key,
        start_byte=start,
        end_byte=end,
        raw_sha256=_sha_bytes(content),
        normalized_sha256=_normalized_digest(content),
        structural_sha256=_structural_digest(content),
        content=content,
        managed_projections=_managed_projections(content),
        reason_code=reason_code,
    )


def _frontmatter_end(text: str, *, byte_prefix: int) -> int | None:
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return byte_prefix
    match = re.search(r"(?m)^---[ \t]*(?:\r?\n|\r|$)", text[4:])
    if match is None:
        return None
    character_end = 4 + match.end()
    return byte_prefix + len(text[:character_end].encode("utf-8"))


def parse_legacy_journal(
    path: str | Path,
    *,
    root: str | Path,
    cohort_id: str,
) -> ParsedLegacyJournalDay:
    """Parse one frozen file into complete, non-overlapping byte dispositions."""

    if not cohort_id or len(cohort_id) > 256:
        raise LegacyJournalImportError("A bounded cohort identity is required")
    source = Path(path)
    inventory = inventory_entry(source, root=Path(root))
    raw = source.expanduser().resolve().read_bytes()
    if inventory.encoding == "invalid":
        spans = (
            _span(
                raw,
                cohort_id=cohort_id,
                inventory=inventory,
                disposition="quarantine",
                section_key=None,
                start=0,
                end=len(raw),
                reason_code="encoding_failure",
            ),
        )
        return _parsed(inventory, cohort_id, spans)
    if inventory.local_date is None:
        spans = (
            _span(
                raw,
                cohort_id=cohort_id,
                inventory=inventory,
                disposition="quarantine",
                section_key=None,
                start=0,
                end=len(raw),
                reason_code="invalid_day_path",
            ),
        )
        return _parsed(inventory, cohort_id, spans)

    bom = 3 if raw.startswith(b"\xef\xbb\xbf") else 0
    text = raw[bom:].decode("utf-8")
    fm_end = _frontmatter_end(text, byte_prefix=bom)
    if fm_end is None:
        spans = (
            _span(
                raw,
                cohort_id=cohort_id,
                inventory=inventory,
                disposition="quarantine",
                section_key=None,
                start=0,
                end=len(raw),
                reason_code="malformed_frontmatter",
            ),
        )
        return _parsed(inventory, cohort_id, spans)

    headings: list[tuple[int, str, str]] = []
    for match in _HEADING.finditer(text):
        contract = _section_contract(match.group(2))
        if contract is None:
            continue
        start = bom + len(text[: match.start()].encode("utf-8"))
        if start < fm_end:
            continue
        headings.append((start, contract[0], contract[1]))

    spans_list: list[LegacyJournalSpan] = []
    cursor = 0
    if fm_end > 0:
        spans_list.append(
            _span(
                raw,
                cohort_id=cohort_id,
                inventory=inventory,
                disposition="frontmatter",
                section_key="frontmatter",
                start=0,
                end=fm_end,
            )
        )
        cursor = fm_end
    if not headings:
        if cursor < len(raw) or not spans_list:
            spans_list.append(
                _span(
                    raw,
                    cohort_id=cohort_id,
                    inventory=inventory,
                    disposition="legacy_day_body",
                    section_key="legacy_day",
                    start=cursor,
                    end=len(raw),
                )
            )
        return _parsed(inventory, cohort_id, tuple(spans_list))
    first = headings[0][0]
    if cursor < first:
        spans_list.append(
            _span(
                raw,
                cohort_id=cohort_id,
                inventory=inventory,
                disposition="static_or_unknown_residue",
                section_key=None,
                start=cursor,
                end=first,
            )
        )
    for index, (start, disposition, section_key) in enumerate(headings):
        end = headings[index + 1][0] if index + 1 < len(headings) else len(raw)
        spans_list.append(
            _span(
                raw,
                cohort_id=cohort_id,
                inventory=inventory,
                disposition=disposition,
                section_key=section_key,
                start=start,
                end=end,
            )
        )
    return _parsed(inventory, cohort_id, tuple(spans_list))


def _parsed(
    inventory: LegacyJournalInventoryEntry,
    cohort_id: str,
    spans: tuple[LegacyJournalSpan, ...],
) -> ParsedLegacyJournalDay:
    _assert_full_coverage(inventory.byte_length, spans)
    receipt = {
        "schema": PARSE_SCHEMA,
        "parserVersion": PARSER_VERSION,
        "cohortId": cohort_id,
        "inventory": inventory.to_dict(),
        "spans": [span.to_receipt() for span in spans],
    }
    return ParsedLegacyJournalDay(
        inventory=inventory,
        cohort_id=cohort_id,
        spans=spans,
        parse_sha256=_sha_json(receipt),
    )


def _assert_full_coverage(
    byte_length: int,
    spans: Sequence[LegacyJournalSpan],
) -> None:
    cursor = 0
    for span in spans:
        if span.start_byte != cursor or span.end_byte < span.start_byte:
            raise LegacyJournalImportError("Journal parser produced an overlap or gap")
        cursor = span.end_byte
    if cursor != byte_length:
        raise LegacyJournalImportError("Journal parser did not disposition every byte")


def parse_inventory_report(
    parsed: Sequence[ParsedLegacyJournalDay],
) -> dict[str, Any]:
    """Aggregate private metadata without returning paths, dates, or prose."""

    dispositions: dict[str, int] = {}
    reasons: dict[str, int] = {}
    managed = 0
    for day in parsed:
        for span in day.spans:
            dispositions[span.disposition] = dispositions.get(span.disposition, 0) + 1
            if span.reason_code:
                reasons[span.reason_code] = reasons.get(span.reason_code, 0) + 1
            managed += len(span.managed_projections)
    return {
        "schema": "wb.legacy-journal-parse-report/v1",
        "parserVersion": PARSER_VERSION,
        "fileCount": len(parsed),
        "byteCount": sum(day.inventory.byte_length for day in parsed),
        "spanCount": sum(len(day.spans) for day in parsed),
        "dispositions": dict(sorted(dispositions.items())),
        "quarantineReasons": dict(sorted(reasons.items())),
        "managedProjectionCount": managed,
        "reportSha256": _sha_json([day.parse_sha256 for day in parsed]),
        "containsProse": False,
    }


__all__ = [
    "INVENTORY_SCHEMA",
    "LegacyJournalImportError",
    "LegacyJournalInventoryEntry",
    "LegacyJournalSpan",
    "LegacyManagedProjection",
    "PARSER_VERSION",
    "PARSE_SCHEMA",
    "ParsedLegacyJournalDay",
    "freeze_inventory",
    "inventory_entry",
    "inventory_sha256",
    "parse_inventory_report",
    "parse_legacy_journal",
    "verify_frozen_inventory",
]
