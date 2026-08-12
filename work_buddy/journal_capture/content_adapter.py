"""Compatibility projection from structured Journal entries to daily Markdown.

The adapter is deliberately the only writer used by the production capture
path.  A stable hidden marker makes a write mechanically reconcilable after a
process dies between the file write and the SQLite acknowledgement.  Identical
user-visible text remains occurrence-safe because identity is the entry ID, not
the formatted line.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from work_buddy.config import load_config
from work_buddy.journal import (
    _effective_minutes,
    current_journal_boundary,
    ensure_journal_exists,
)
from work_buddy.journal_capture.models import (
    CaptureTarget,
    JournalEntry,
    JournalProjectionDiverged,
    JournalProjectionError,
)


_WRITE_LOCK = threading.RLock()
_TOP_LEVEL_RE = re.compile(r"^(?:\ufeff)?#\s+", re.MULTILINE)
_LOG_HEADER_RE = re.compile(
    r"^(?:\ufeff)?#\s+\*{0,2}Log\*{0,2}[ \t]*(?=\r?$)", re.MULTILINE
)
_RUNNING_HEADER_RE = re.compile(
    r"^(?:\ufeff)?#\s+\*{0,2}Running Notes\s*/\s*Considerations\*{0,2}"
    r"[ \t]*(?=\r?$)",
    re.MULTILINE,
)
_RUNNING_END_RE = re.compile(
    r"^%\s*RUNNING\s+END[ \t]*(?=\r?$)", re.MULTILINE
)
_LOG_TIME_RE = re.compile(r"^\*\s+(\d{1,2}:\d{2}\s+[AP]M)\s+-", re.IGNORECASE)


@dataclass(frozen=True)
class ProjectionResult:
    status: str
    base_sha256: str
    result_sha256: str
    recovered_existing_marker: bool
    path: Path


@dataclass(frozen=True)
class RedactionProjectionResult:
    status: str
    base_sha256: str
    result_sha256: str
    recovered_existing_marker: bool
    path: Path


@dataclass(frozen=True)
class JournalSectionSnapshot:
    day_id: str
    path: Path
    content: str
    file_sha256: str
    log_bounds: tuple[int, int] | None
    running_notes_bounds: tuple[int, int] | None

    def section(self, kind: str) -> tuple[str, int, int]:
        bounds = (
            self.log_bounds if kind == "logical_day_log" else self.running_notes_bounds
        )
        if bounds is None:
            raise JournalProjectionError("The requested Journal section is unavailable.")
        return self.content[bounds[0] : bounds[1]], bounds[0], bounds[1]


def marker_for(entry_id: str, content_sha256: str) -> str:
    return (
        f"<!-- wb:journal-entry/v1 id={entry_id} "
        f"content-sha256={content_sha256} -->"
    )


def closing_marker_for(entry_id: str) -> str:
    return f"<!-- /wb:journal-entry/v1 id={entry_id} -->"


def redacted_marker_for(entry_id: str, redaction_event_id: str) -> str:
    return (
        f"<!-- wb:journal-entry-redacted/v1 id={entry_id} "
        f"redaction-event={redaction_event_id} -->"
    )


class JournalContentAdapter:
    """Only production seam for daily-note content reads and section writes.

    Before an entity cuts over, Markdown remains authoritative.  Once the
    document-kernel binding owns an entity, this adapter permits changes only
    through that binding's section-CAS projection; unrelated daily-note
    sections remain byte-for-byte outside Journal prose authority.
    """

    def __init__(
        self,
        vault_root: str | Path | None = None,
        *,
        journal_dir: str | Path | None = None,
    ) -> None:
        if vault_root is None:
            configured = (load_config() or {})["vault_root"]
        else:
            configured = vault_root
        self.vault_root = Path(configured).expanduser().resolve()
        configured_dir = journal_dir if journal_dir is not None else "journal"
        relative = Path(configured_dir)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError("Journal directory must be vault-relative")
        self.journal_dir = relative

    def journal_path(self, day_id: str) -> Path:
        return self.vault_root / self.journal_dir / f"{day_id}.md"

    def snapshot(self, day_id: str) -> JournalSectionSnapshot:
        path = self.journal_path(day_id)
        if not path.is_file():
            raise JournalProjectionError("The Journal day is unavailable.")
        content = self._read(path, day_id)
        return JournalSectionSnapshot(
            day_id=day_id,
            path=path,
            content=content,
            file_sha256=_sha(content),
            log_bounds=_section_body_bounds(content, _LOG_HEADER_RE),
            running_notes_bounds=_section_body_bounds(
                content, _RUNNING_HEADER_RE, running=True
            ),
        )

    def read_day(self, day_id: str) -> str:
        snapshot = self.snapshot(day_id)
        content = snapshot.content
        try:
            from work_buddy.paths import resolve

            journal_store_path = resolve("db/journal-capture")
            if not journal_store_path.is_file():
                return content
            from work_buddy.document_kernel.causality import DocumentCausalityStore
            from work_buddy.document_kernel.domain_service import DomainContentStoreManager
            from work_buddy.journal_capture.models import JournalMigrationState
            from work_buddy.journal_capture.store import JournalCaptureStore
            from work_buddy.truth import documents

            records = JournalCaptureStore(
                journal_store_path,
                read_only=True,
            ).migrations_for_day(day_id)
            authoritative = tuple(
                record
                for record in records
                if record.mirrored_state
                in {
                    JournalMigrationState.COWORK,
                    JournalMigrationState.PAUSED_DIVERGED,
                }
                and record.binding_id is not None
            )
            if not authoritative:
                return content
            store = DomainContentStoreManager().open_existing(self.vault_root)
            causality = DocumentCausalityStore(store.paths.sidecar)
            for record in authoritative:
                binding = causality.get_binding(record.binding_id)
                if (
                    binding is None
                    or binding.lifecycle != "current"
                    or binding.content_authority != "co_work"
                ):
                    continue
                document = documents.get_document(store, binding.document_id)
                authoritative = store.resolve_blob_path(
                    f"blobs/{document.content_sha256}"
                ).read_text(encoding="utf-8")
                content = _overlay_managed_body(
                    content,
                    marker_id=record.marker_id,
                    replacement=authoritative,
                )
        except (KeyError, OSError, RuntimeError, UnicodeError):
            # A known authority cannot silently fall back to stale Markdown.
            # If there are no migration records, the fast path above returns.
            raise JournalProjectionError(
                "The authoritative Journal content is temporarily unavailable."
            )
        return content

    def read_log(self, day_id: str) -> str:
        content = self.read_day(day_id)
        bounds = _section_body_bounds(content, _LOG_HEADER_RE)
        if bounds is None:
            raise JournalProjectionError("The Journal Log is unavailable.")
        return content[bounds[0] : bounds[1]]

    def read_running_notes(self, day_id: str) -> str:
        content = self.read_day(day_id)
        bounds = _section_body_bounds(content, _RUNNING_HEADER_RE, running=True)
        if bounds is None:
            raise JournalProjectionError("Running Notes are unavailable.")
        return content[bounds[0] : bounds[1]]

    def create_day_if_absent(
        self,
        day_id: str,
        *,
        content: str,
        content_hint: str,
    ) -> JournalSectionSnapshot:
        """Create a compatibility daily note only while it is genuinely absent.

        This is the creation counterpart to ``write_day_cas``.  It keeps
        section fixers behind the same bridge/fallback policy without turning
        absence into permission to replace a concurrently-created note.
        """

        path = self.journal_path(day_id)
        with _WRITE_LOCK:
            if path.exists():
                return self.snapshot(day_id)
            self._write(
                path,
                day_id,
                content,
                content_hint,
                write_mode="replace",
            )
            confirmed = self.snapshot(day_id)
            if confirmed.content != content:
                raise JournalProjectionError(
                    "The new Journal day could not be verified."
                )
            return confirmed

    def write_day_cas(
        self,
        day_id: str,
        *,
        expected_file_sha256: str,
        content: str,
        content_hint: str | None,
        write_mode: str = "replace",
    ) -> str:
        """Replace a daily file only from the exact observed base.

        The shared vault writer additionally checks that Co-work-owned managed
        blocks did not change, catching callers that still operate on whole
        files while allowing Sign-In/planner/unknown sections to evolve.
        """

        path = self.journal_path(day_id)
        with _WRITE_LOCK:
            current = self._read(path, day_id)
            if _sha(current) != expected_file_sha256:
                raise JournalProjectionDiverged(
                    "The Journal changed before this update could be applied."
                )
            if current == content:
                return expected_file_sha256
            self._write(
                path,
                day_id,
                content,
                content_hint or "wb:journal-content-adapter/v1",
                write_mode=write_mode,
            )
            confirmed = self._read(path, day_id)
            if confirmed != content:
                raise JournalProjectionError("The Journal update could not be verified.")
            return _sha(confirmed)

    def install_managed_selection(
        self,
        *,
        day_id: str,
        marker_id: str,
        expected_file_sha256: str,
        expected_section_sha256: str,
        selection_start: int,
        selection_end: int,
    ) -> JournalSectionSnapshot:
        """Fence one reviewed legacy slice with stable projection markers.

        Offsets and both hashes are rechecked under the adapter lock.  A retry
        recognizes the exact marker pair, while any external edit fails closed.
        """

        if not re.fullmatch(r"[0-9a-f]{32}", marker_id):
            raise JournalProjectionError("The Journal migration marker is invalid.")
        path = self.journal_path(day_id)
        opening_prefix = f"<!-- wb:journal-entry/v1 id={marker_id} "
        closing = closing_marker_for(marker_id)
        with _WRITE_LOCK:
            current = self._read(path, day_id)
            if opening_prefix in current and closing in current:
                matches = [
                    match
                    for match in _MANAGED_BLOCK_RE.finditer(current)
                    if match.group("id") == marker_id
                ]
                if len(matches) != 1:
                    raise JournalProjectionDiverged(
                        "The Journal migration boundary is ambiguous."
                    )
                match = matches[0]
                if match.group("digest") != expected_section_sha256:
                    raise JournalProjectionDiverged(
                        "The Journal migration boundary no longer matches its selection."
                    )
                body = match.group("body")
                if "<!-- wb:cowork-projection/v1 " not in body:
                    candidates = {body}
                    if body.endswith("\r\n"):
                        candidates.add(body[:-2])
                    elif body.endswith("\n"):
                        candidates.add(body[:-1])
                    if not any(
                        _sha(candidate) == expected_section_sha256
                        for candidate in candidates
                    ):
                        raise JournalProjectionDiverged(
                            "The selected Journal passage changed before cutover."
                        )
                return self.snapshot(day_id)
            if _sha(current) != expected_file_sha256:
                raise JournalProjectionDiverged(
                    "The selected Journal passage changed before cutover."
                )
            if selection_start < 0 or selection_end <= selection_start or selection_end > len(current):
                raise JournalProjectionDiverged("The selected Journal passage is unavailable.")
            selected = current[selection_start:selection_end]
            if _sha(selected) != expected_section_sha256:
                raise JournalProjectionDiverged(
                    "The selected Journal passage changed before cutover."
                )
            opening = marker_for(marker_id, expected_section_sha256)
            newline = "\r\n" if "\r\n" in current else "\n"
            updated = (
                current[:selection_start]
                + opening
                + newline
                + selected
                + ("" if selected.endswith(("\n", "\r")) else newline)
                + closing
                + newline
                + current[selection_end:]
            )
            self._write(
                path,
                day_id,
                updated,
                opening,
                write_mode="replace",
                journal_owned_write=True,
            )
            confirmed = self._read(path, day_id)
            if opening not in confirmed or closing not in confirmed:
                raise JournalProjectionError(
                    "The Journal migration boundary could not be verified."
                )
            return JournalSectionSnapshot(
                day_id=day_id,
                path=path,
                content=confirmed,
                file_sha256=_sha(confirmed),
                log_bounds=_section_body_bounds(confirmed, _LOG_HEADER_RE),
                running_notes_bounds=_section_body_bounds(
                    confirmed, _RUNNING_HEADER_RE, running=True
                ),
            )

    def unwrap_managed_selection(
        self,
        *,
        day_id: str,
        marker_id: str,
        expected_file_sha256: str,
    ) -> JournalSectionSnapshot:
        """Return a rolled-back entity to editable Markdown authority.

        The caller must fence the canonical Co-work epoch first.  This method
        then removes both ownership marker layers from the exact observed file;
        retries after a landed write are harmless.
        """

        path = self.journal_path(day_id)
        closing = closing_marker_for(marker_id)
        opening_re = re.compile(
            rf"<!-- wb:journal-entry/v1 id={re.escape(marker_id)} "
            rf"content-sha256=[0-9a-f]{{64}} -->\r?\n"
        )
        with _WRITE_LOCK:
            current = self._read(path, day_id)
            opening = opening_re.search(current)
            if opening is None and closing not in current:
                return self.snapshot(day_id)
            if _sha(current) != expected_file_sha256:
                raise JournalProjectionDiverged(
                    "The Journal changed before rollback could finish."
                )
            if opening is None or current.count(closing) != 1:
                raise JournalProjectionDiverged(
                    "The Journal rollback boundary is ambiguous."
                )
            close_at = current.index(closing, opening.end())
            body = current[opening.end() : close_at]
            inner = re.fullmatch(
                r"<!-- wb:cowork-projection/v1 [^\r\n]+ -->\r?\n"
                r"(?P<markdown>.*?)"
                r"<!-- /wb:cowork-projection/v1 [^\r\n]+ -->\r?\n?",
                body,
                re.DOTALL,
            )
            replacement = inner.group("markdown") if inner is not None else body
            end = close_at + len(closing)
            if current.startswith("\r\n", end):
                end += 2
            elif end < len(current) and current[end] in {"\r", "\n"}:
                end += 1
            updated = current[: opening.start()] + replacement + current[end:]
            self._write(
                path,
                day_id,
                updated,
                "wb:journal-rollback/v1",
                write_mode="replace",
                journal_owned_write=True,
            )
            confirmed = self._read(path, day_id)
            if opening_re.search(confirmed) is not None or closing in confirmed:
                raise JournalProjectionError("The Journal rollback could not be verified.")
            return JournalSectionSnapshot(
                day_id=day_id,
                path=path,
                content=confirmed,
                file_sha256=_sha(confirmed),
                log_bounds=_section_body_bounds(confirmed, _LOG_HEADER_RE),
                running_notes_bounds=_section_body_bounds(
                    confirmed, _RUNNING_HEADER_RE, running=True
                ),
            )

    def redact_managed_selection(
        self,
        *,
        day_id: str,
        marker_id: str,
        redaction_event_id: str,
        expected_body_sha256: str,
    ) -> RedactionProjectionResult:
        """Remove one exact migration-owned section and retain a tombstone."""

        path = self.journal_path(day_id)
        replacement = redacted_marker_for(marker_id, redaction_event_id)
        with _WRITE_LOCK:
            current = self._read(path, day_id)
            base_sha = _sha(current)
            if replacement in current:
                return RedactionProjectionResult(
                    "committed", base_sha, base_sha, True, path
                )
            matches = [
                match
                for match in _MANAGED_BLOCK_RE.finditer(current)
                if match.group("id") == marker_id
            ]
            if len(matches) != 1 or _sha(matches[0].group("body")) != expected_body_sha256:
                raise JournalProjectionDiverged(
                    "The managed Journal passage changed before source redaction."
                )
            match = matches[0]
            start, end = match.span()
            if start > 0 and current[start - 1] == "\n":
                start -= 1
            if end < len(current) and current[end] == "\n":
                end += 1
            updated = current[:start] + "\n" + replacement + "\n" + current[end:]
            self._write(
                path,
                day_id,
                updated,
                replacement,
                write_mode="replace",
                journal_owned_write=True,
            )
            confirmed = self._read(path, day_id)
            if replacement not in confirmed or any(
                item.group("id") == marker_id
                for item in _MANAGED_BLOCK_RE.finditer(confirmed)
            ):
                raise JournalProjectionError(
                    "The Journal source redaction could not be verified."
                )
            return RedactionProjectionResult(
                "committed", base_sha, _sha(confirmed), False, path
            )

    def append(self, entry: JournalEntry, *, stated_at: str | None = None) -> ProjectionResult:
        if entry.entry_kind not in (CaptureTarget.LOG, CaptureTarget.RUNNING_NOTES):
            raise JournalProjectionError("Journal cannot project an unresolved destination.")
        path = self.journal_path(entry.day_id)
        if not path.exists():
            result = ensure_journal_exists(self.vault_root, entry.day_id, create=True)
            if not result.get("exists"):
                raise JournalProjectionError("The Journal day could not be opened for writing.")

        with _WRITE_LOCK:
            current = self._read(path, entry.day_id)
            base_sha = _sha(current)
            expected_marker = marker_for(entry.entry_id, entry.content_sha256)
            closing = closing_marker_for(entry.entry_id)

            marker_present = expected_marker in current and closing in current
            if marker_present:
                return ProjectionResult(
                    status="committed",
                    base_sha256=base_sha,
                    result_sha256=base_sha,
                    recovered_existing_marker=True,
                    path=path,
                )
            if f"id={entry.entry_id} " in current:
                raise JournalProjectionError(
                    "The managed Journal marker exists with a different digest."
                )

            block = self._render(entry, stated_at=stated_at)
            if entry.entry_kind is CaptureTarget.LOG:
                log_bounds = _section_body_bounds(current, _LOG_HEADER_RE)
                log_body = (
                    "" if log_bounds is None else current[log_bounds[0] : log_bounds[1]]
                )
                if log_body and "<!-- wb:cowork-projection/v1 " in log_body:
                    raise JournalProjectionDiverged(
                        "The logical-day Log is Co-work-owned; append through its bound document."
                    )
                updated = self._insert_log(current, block, stated_at=stated_at)
            else:
                updated = self._insert_running_note(current, block)
            result_sha = _sha(updated)
            self._write(path, entry.day_id, updated, expected_marker)
            confirmed = self._read(path, entry.day_id)
            if expected_marker not in confirmed or closing not in confirmed:
                raise JournalProjectionError(
                    "The Journal write could not be verified by its stable marker."
                )
            return ProjectionResult(
                status="committed",
                base_sha256=base_sha,
                result_sha256=_sha(confirmed),
                recovered_existing_marker=False,
                path=path,
            )

    def marker_is_present(self, entry: JournalEntry) -> bool:
        path = self.journal_path(entry.day_id)
        if not path.is_file():
            return False
        content = self._read(path, entry.day_id)
        return (
            marker_for(entry.entry_id, entry.content_sha256) in content
            and closing_marker_for(entry.entry_id) in content
        )

    def redact(
        self,
        entry: JournalEntry,
        *,
        redaction_event_id: str,
    ) -> RedactionProjectionResult:
        """Remove one exact managed block without touching nearby user prose.

        A stable content-free replacement marker makes a crash after the file
        write but before the Journal receipt recoverable.  Missing or
        ambiguous boundaries fail closed: the Sources maintenance intent stays
        pending instead of claiming that readable content was removed.
        """

        path = self.journal_path(entry.day_id)
        if not path.is_file():
            raise JournalProjectionDiverged(
                "The managed Journal file is unavailable for source redaction."
            )
        replacement = redacted_marker_for(entry.entry_id, redaction_event_id)
        with _WRITE_LOCK:
            current = self._read(path, entry.day_id)
            base_sha = _sha(current)
            if replacement in current:
                return RedactionProjectionResult(
                    status="committed",
                    base_sha256=base_sha,
                    result_sha256=base_sha,
                    recovered_existing_marker=True,
                    path=path,
                )

            opening = entry.projection_marker
            closing = closing_marker_for(entry.entry_id)
            if current.count(opening) != 1 or current.count(closing) != 1:
                raise JournalProjectionDiverged(
                    "The managed Journal passage changed before source redaction."
                )
            start = current.index(opening)
            end = current.index(closing, start) + len(closing)
            if end <= start:
                raise JournalProjectionDiverged(
                    "The managed Journal passage boundaries are invalid."
                )
            # Consume one adjacent line break on either side so removing the
            # managed block does not leave a growing stack of blank lines.
            if start > 0 and current[start - 1] == "\n":
                start -= 1
            if end < len(current) and current[end] == "\n":
                end += 1
            updated = current[:start] + "\n" + replacement + "\n" + current[end:]
            self._write(
                path,
                entry.day_id,
                updated,
                replacement,
                write_mode="replace",
            )
            confirmed = self._read(path, entry.day_id)
            if replacement not in confirmed or opening in confirmed or closing in confirmed:
                raise JournalProjectionError(
                    "The Journal source redaction could not be verified."
                )
            return RedactionProjectionResult(
                status="committed",
                base_sha256=base_sha,
                result_sha256=_sha(confirmed),
                recovered_existing_marker=False,
                path=path,
            )

    def _render(self, entry: JournalEntry, *, stated_at: str | None) -> str:
        opening = marker_for(entry.entry_id, entry.content_sha256)
        closing = closing_marker_for(entry.entry_id)
        markdown = entry.markdown.rstrip("\n")
        if entry.entry_kind is CaptureTarget.RUNNING_NOTES:
            body = markdown
        else:
            display_time = _display_time(stated_at or entry.created_at)
            lines = markdown.splitlines() or [""]
            first = lines[0]
            body_lines = [f"* {display_time} - {first} #wb/journal/log"]
            body_lines.extend(f"  {line}" if line else "" for line in lines[1:])
            body = "\n".join(body_lines)
        return f"{opening}\n{body}\n{closing}"

    def _insert_log(self, content: str, block: str, *, stated_at: str | None) -> str:
        bounds = _section_body_bounds(content, _LOG_HEADER_RE)
        if bounds is None:
            raise JournalProjectionError("The daily note has no Log section.")
        start, end = bounds
        newline = "\r\n" if "\r\n" in content else "\r" if "\r" in content else "\n"
        block = _with_newline_style(block, newline)
        body = content[start:end]
        insertion = len(body)
        new_time = _display_time(stated_at)
        new_minutes = _effective_minutes(new_time, current_journal_boundary())
        offset = 0
        lines = body.splitlines(keepends=True)
        for index, line in enumerate(lines):
            match = _LOG_TIME_RE.match(line.strip())
            if match is not None:
                existing = _effective_minutes(match.group(1), current_journal_boundary())
                if new_minutes >= 0 and existing > new_minutes:
                    insertion = offset
                    if index > 0 and "<!-- wb:journal-entry/v1 " in lines[index - 1]:
                        insertion -= len(lines[index - 1])
                    break
            offset += len(line)
        before = body[:insertion].rstrip("\r\n")
        after = body[insertion:].lstrip("\r\n")
        pieces = [piece for piece in (before, block, after) if piece]
        new_body = newline + newline.join(pieces) + newline
        return content[:start] + new_body + content[end:]

    def _insert_running_note(self, content: str, block: str) -> str:
        bounds = _section_body_bounds(content, _RUNNING_HEADER_RE, running=True)
        if bounds is None:
            raise JournalProjectionError(
                "The daily note has no Running Notes / Considerations section."
            )
        start, end = bounds
        newline = "\r\n" if "\r\n" in content else "\r" if "\r" in content else "\n"
        block = _with_newline_style(block, newline)
        body = content[start:end].strip("\r\n")
        new_body = (
            newline
            + block
            + (newline * 2 + body if body else "")
            + newline
        )
        return content[:start] + new_body + content[end:]

    def _read(self, path: Path, day_id: str) -> str:
        rel = f"{self.journal_dir.as_posix()}/{day_id}.md"
        try:
            from work_buddy.obsidian.bridge import is_available, read_file_raw

            if is_available():
                result = read_file_raw(rel)
                if result is not None:
                    return result
        except ImportError:
            pass
        try:
            # ``Path.read_text`` uses universal-newline translation.  Journal
            # migration parity is defined over the exact selected bytes, so
            # decode the bytes directly and retain CRLF plus a possible BOM.
            return path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise JournalProjectionError("The daily note could not be read.") from exc

    def _write(
        self,
        path: Path,
        day_id: str,
        content: str,
        marker: str,
        *,
        write_mode: str = "insert",
        journal_owned_write: bool = False,
    ) -> None:
        from work_buddy.obsidian.vault_writer import vault_write

        try:
            values = {
                "write_mode": write_mode,
                "content_hint": marker,
            }
            if journal_owned_write:
                values["journal_owned_write"] = True
            ok = vault_write(
                f"{self.journal_dir.as_posix()}/{day_id}.md",
                path,
                content,
                **values,
            )
        except Exception as exc:
            from work_buddy.obsidian.errors import ObsidianError

            if isinstance(exc, ObsidianError):
                raise
            # Typed bridge exceptions retain their type in the cause without
            # leaking captured text into the operational error.
            raise JournalProjectionError("The daily note write did not complete.") from exc
        if not ok:
            raise JournalProjectionError("The daily note write did not complete.")


def _section_body_bounds(
    content: str,
    header_re: re.Pattern[str],
    *,
    running: bool = False,
) -> tuple[int, int] | None:
    match = header_re.search(content)
    if match is None:
        return None
    start = match.end()
    if content.startswith("\r\n", start):
        start += 2
    elif start < len(content) and content[start] in {"\r", "\n"}:
        start += 1
    if running:
        end_marker = _RUNNING_END_RE.search(content, start)
        if end_marker is not None:
            return start, end_marker.start()
    next_header = _TOP_LEVEL_RE.search(content, start)
    return start, next_header.start() if next_header is not None else len(content)


def _display_time(value: str | None) -> str:
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone().strftime("%-I:%M %p")
        except (ValueError, OSError):
            try:
                return parsed.astimezone().strftime("%I:%M %p").lstrip("0")
            except (UnboundLocalError, ValueError, OSError):
                pass
    return datetime.now().strftime("%I:%M %p").lstrip("0")


def _with_newline_style(value: str, newline: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_MANAGED_BLOCK_RE = re.compile(
    r"(?P<open><!-- wb:journal-entry/v1 id=(?P<id>[0-9a-f]{32}) "
    r"content-sha256=(?P<digest>[0-9a-f]{64}) -->\r?\n)"
    r"(?P<body>.*?)"
    r"(?P<close><!-- /wb:journal-entry/v1 id=(?P=id) -->)",
    re.DOTALL,
)


def assert_cowork_owned_sections_unchanged(
    before: str,
    after: str,
    *,
    day_id: str | None = None,
) -> None:
    """Reject a generic whole-file write that mutates Co-work-owned prose.

    Projection output carries an inner ``wb:cowork-projection`` marker.  The
    full outer block is therefore a self-describing ownership boundary that a
    generic Journal writer can preserve without opening migration databases.
    Missing, added, duplicated, or changed owned blocks all fail closed.
    """

    def owned(value: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for match in _MANAGED_BLOCK_RE.finditer(value):
            block = match.group(0)
            if "<!-- wb:cowork-projection/v1 " not in match.group("body"):
                continue
            marker_id = match.group("id")
            if marker_id in result:
                raise JournalProjectionDiverged(
                    "A Co-work-owned Journal boundary is ambiguous."
                )
            result[marker_id] = block
        return result

    before_owned = owned(before)
    after_owned = owned(after)
    if before_owned != after_owned:
        raise JournalProjectionDiverged(
            "This update would change Co-work-owned Journal prose. Open that content "
            "in Co-work or roll its authority back first."
        )
    if day_id is not None:
        log_id = hashlib.sha256(
            f"journal-log/v1\0{day_id}".encode("utf-8")
        ).hexdigest()[:32]
        if log_id in before_owned:
            before_bounds = _section_body_bounds(before, _LOG_HEADER_RE)
            after_bounds = _section_body_bounds(after, _LOG_HEADER_RE)
            if (
                before_bounds is None
                or after_bounds is None
                or before[before_bounds[0] : before_bounds[1]]
                != after[after_bounds[0] : after_bounds[1]]
            ):
                raise JournalProjectionDiverged(
                    "The logical-day Log is Co-work-owned. Open it in Co-work "
                    "or roll its authority back first."
                )


def _overlay_managed_body(content: str, *, marker_id: str, replacement: str) -> str:
    matches = [
        match for match in _MANAGED_BLOCK_RE.finditer(content) if match.group("id") == marker_id
    ]
    if len(matches) != 1:
        raise JournalProjectionDiverged(
            "The authoritative Journal projection boundary is unavailable."
        )
    match = matches[0]
    newline = "\r\n" if "\r\n" in match.group(0) else "\n"
    body = replacement
    if body and not body.endswith(("\n", "\r")):
        body += newline
    return content[: match.start("body")] + body + content[match.end("body") :]


__all__ = [
    "JournalContentAdapter",
    "JournalSectionSnapshot",
    "ProjectionResult",
    "RedactionProjectionResult",
    "assert_cowork_owned_sections_unchanged",
    "closing_marker_for",
    "marker_for",
    "redacted_marker_for",
]
