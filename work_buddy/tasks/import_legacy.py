"""Dry-run-first legacy task import and guarded cohort operator.

Normal task APIs cannot call this module.  It is an operator surface for the
one-way legacy import and the maintenance-window authority transition.  The
CLI defaults to inventory only; shadow writes require ``--apply-shadow`` and
authority activation additionally requires an exact in-process confirmation
token through :class:`LegacyTaskCutoverOperator`.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.document_kernel.file_provider import WorkBuddyFileImportProvider
from work_buddy.document_kernel.protocol import sha256_bytes, structured_head_sha256
from work_buddy.cowork import provenance
from work_buddy.sources import (
    ActorRef,
    OriginRef,
    ProviderRegistry,
    SourceError,
    SourceRef,
    SourceStore,
    resolve_and_reserve_source,
    resolve_source,
    source_capture_from_origin,
)
from work_buddy.tasks.documents import TaskDocumentStoreManager
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.registry import TruthStoreRegistry

from .migration import (
    CohortStateError,
    CutoverPreconditionError,
    InventoryItem,
    LegacyInventory,
    LegacyInventoryError,
    LegacyManifestEntry,
    LegacyTaskInventoryBuilder,
    TaskMigrationLedger,
    canonical_json,
    canonical_sha256,
    normalized_markdown_sha256,
)
from .store import TaskStore


ACTIVATION_CONFIRMATION = "ACTIVATE_NATIVE_TASK_AUTHORITY"
_IMPORTABLE_DOCUMENT_CLASSES = frozenset(
    {
        "task_note_live",
        "task_note_live_db_only",
        "task_note_idless",
        "task_note_deleted",
        "recovered_task_document",
    }
)
_LOCAL_FILE_CLASSES = frozenset({"local_file_pdf", "local_file_sensitive"})
_WIKI_EMBED_RE = re.compile(r"!\[\[([^\]|#]+)([^\]]*)\]\]")
_MARKDOWN_LINK_RE = re.compile(r"(!?\[[^\]]*\]\()([^\s)]+)(\s*(?:\"[^\"]*\")?\))")
_INLINE_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]+)\]\(([^)\n]+)\)")


def _newline_style(value: bytes) -> str:
    if b"\r\n" in value:
        return "crlf"
    if b"\n" in value:
        return "lf"
    if b"\r" in value:
        return "cr"
    return "none"


def _trailing_newlines(value: bytes) -> int:
    text = value.decode("utf-8-sig")
    count = 0
    cursor = len(text)
    while cursor:
        if text[:cursor].endswith("\r\n"):
            cursor -= 2
        elif text[cursor - 1] in "\r\n":
            cursor -= 1
        else:
            break
        count += 1
    return count


def _title(value: bytes, fallback: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", value.decode("utf-8-sig"), re.MULTILINE)
    return match.group(1).strip() if match else fallback


def _kernel_projection_equivalent(projection: bytes, expected: bytes) -> bool:
    """Compare a kernel projection without weakening content parity.

    The structured kernel canonicalizes Markdown presentation while preserving
    the same document: it inserts empty block separators, emits HTML entities,
    and normalizes Markdown escape markers. Exact legacy bytes remain retained
    in Sources; this comparison accepts only those deterministic presentation
    changes and still requires BOM, newline style, trailing-newline count,
    semantic text/token order, and non-ASCII symbols such as emoji to match.
    """

    if projection.startswith(b"\xef\xbb\xbf") != expected.startswith(
        b"\xef\xbb\xbf"
    ):
        return False
    if _newline_style(projection) != _newline_style(expected):
        return False
    if _trailing_newlines(projection) != _trailing_newlines(expected):
        return False

    def canonical(value: bytes) -> bytes:
        text = value.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        text = html.unescape(text)
        text = re.sub(r"\\([\\`*{}\[\]()#+\-.!_|>~])", r"\1", text)
        text = re.sub(r"\\(?=\n|$)", "", text)
        return "\n".join(line for line in text.split("\n") if line != "").encode(
            "utf-8"
        )

    canonical_projection = canonical(projection)
    canonical_expected = canonical(expected)
    if canonical_projection == canonical_expected:
        return True

    def semantic_tokens(value: bytes) -> tuple[str, ...]:
        text = unicodedata.normalize("NFC", value.decode("utf-8"))
        # Markdown table serializers may add padding and missing outer/cell
        # separators.  Within a syntactic table row, an unescaped pipe is
        # presentation structure rather than document text.
        text = "\n".join(
            line.replace("|", " ")
            if line.lstrip().startswith("|") and line.rstrip().endswith("|")
            else line
            for line in text.split("\n")
        )

        def collapse_generated_autolink(match: re.Match[str]) -> str:
            label = match.group(1)
            destination = match.group(2).strip()
            if destination == label or destination == f"mailto:{label}":
                return label
            return match.group(0)

        # The kernel serializes bare URL/email autolinks as explicit Markdown.
        # Discard only the duplicated destination when it conveys exactly the
        # same value as the label; ordinary named links retain both values.
        text = _INLINE_LINK_RE.sub(collapse_generated_autolink, text)
        tokens: list[str] = []
        word: list[str] = []

        def word_character(character: str) -> bool:
            category = unicodedata.category(character)
            return character.isalnum() or category.startswith("M")

        def flush() -> None:
            if word:
                tokens.append("word:" + "".join(word))
                word.clear()

        index = 0
        connector_characters = "./\\:_-@?&#|"
        while index < len(text):
            character = text[index]
            category = unicodedata.category(character)
            if word_character(character):
                word.append(character)
                index += 1
                continue
            flush()
            if character in connector_characters:
                end = index + 1
                while end < len(text) and text[end] in connector_characters:
                    end += 1
                if (
                    index > 0
                    and end < len(text)
                    and word_character(text[index - 1])
                    and word_character(text[end])
                ):
                    tokens.append("connector:" + text[index:end])
                index = end
                continue
            if (
                character in "<>=+~$%^"
                or (ord(character) > 127 and category.startswith("S"))
            ):
                tokens.append("symbol:" + character)
            index += 1
        flush()
        return tuple(tokens)

    return semantic_tokens(canonical_projection) == semantic_tokens(canonical_expected)


def _literal_markdown_envelope(value: bytes) -> bytes:
    """Wrap legacy text in a lossless code block for malformed-Markdown fallback.

    Some legacy notes contain unbalanced fences or nested list/quote constructs
    that a CommonMark parser is permitted to reinterpret or discard.  Exact
    source bytes remain in Sources; this envelope gives the Co-work document a
    stable, editable literal rendering without dropping any of their content.
    """

    has_bom = value.startswith(b"\xef\xbb\xbf")
    text = value.decode("utf-8-sig")
    style = _newline_style(value)
    separator = {"crlf": "\r\n", "cr": "\r"}.get(style, "\n")
    longest_fence = max((len(run) for run in re.findall(r"`+", text)), default=0)
    marker = "`" * max(3, longest_fence + 1)
    trailing = separator * _trailing_newlines(value)
    # The separator immediately before the closing fence is structural.  Keep
    # the original trailing newlines inside the fenced payload as well so an
    # extractor can recover the exact source bytes instead of inferring them
    # from presentation metadata.
    encoded = f"{marker}{separator}{text}{separator}{marker}{trailing}".encode(
        "utf-8"
    )
    return (b"\xef\xbb\xbf" + encoded) if has_bom else encoded


def _literal_projection_equivalent(projection: bytes, expected: bytes) -> bool:
    """Recover a literal fallback payload and compare exact source bytes.

    The kernel may shorten the outer fence marker while retaining the code
    block payload.  It may not alter the payload itself.  The second candidate
    supports documents produced by the original envelope implementation,
    which represented source trailing newlines after the closing fence.
    """

    expected_bom = expected.startswith(b"\xef\xbb\xbf")
    if projection.startswith(b"\xef\xbb\xbf") != expected_bom:
        return False
    raw = projection[3:] if expected_bom else projection
    first_break = re.search(br"\r\n|\r|\n", raw)
    if first_break is None:
        return False
    opening = raw[: first_break.start()]
    separator = first_break.group(0)
    if re.fullmatch(br"`{3,}", opening) is None:
        return False

    core = raw
    outer_tail = b""
    for _ in range(_trailing_newlines(expected)):
        if not core.endswith(separator):
            return False
        core = core[: -len(separator)]
        outer_tail = separator + outer_tail

    closing_break = core.rfind(separator)
    if closing_break < first_break.end():
        return False
    closing = core[closing_break + len(separator) :]
    if closing != opening or re.fullmatch(br"`{3,}", closing) is None:
        return False
    payload = core[first_break.end() : closing_break]
    if expected_bom:
        payload = b"\xef\xbb\xbf" + payload

    # v2 stores trailing bytes inside the payload.  The legacy envelope stored
    # them after the closing fence; accepting that representation is a strict,
    # reversible compatibility path rather than a semantic-token relaxation.
    return payload == expected or payload + outer_tail == expected


def _projection_matches_strategy(
    projection: bytes,
    expected: bytes,
    *,
    strategy: str,
) -> bool:
    if strategy.startswith("literal_markdown_fallback"):
        return _literal_projection_equivalent(projection, expected)
    return _kernel_projection_equivalent(projection, expected)


def _document_id(note_uuid: str) -> str:
    return hashlib.sha256(
        f"work-buddy-task-import/note-document/v1\0{note_uuid}".encode("utf-8")
    ).hexdigest()[:32]


def _document_path(note_uuid: str) -> str:
    token = hashlib.sha256(
        f"work-buddy-task-import/note-path/v1\0{note_uuid}".encode("utf-8")
    ).hexdigest()[:32]
    return f"tasks/imported/{token}.cowork"


def _binding_id(task_id: str, store_id: str, document_id: str) -> str:
    identity = "\0".join(
        ("tasks", "task_knowledge", task_id, "task_knowledge", store_id, document_id)
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _local_root_id(manifest_sha256: str) -> str:
    return "root_" + hashlib.sha256(
        f"work-buddy-frozen-task-root/v1\0{manifest_sha256}".encode("utf-8")
    ).hexdigest()[:32]


def _local_link_id(root_id: str, document_id: str, item: InventoryItem) -> str:
    assert item.relative_path and item.content_sha256
    return "lf_" + hashlib.sha256(
        f"work-buddy-local-link/v2\0{root_id}\0{document_id}\0{item.relative_path}\0{item.content_sha256}".encode(
            "utf-8"
        )
    ).hexdigest()[:32]


def _resolve_reference(
    note_path: str,
    target: str,
    attachments: Sequence[InventoryItem],
) -> InventoryItem | None:
    clean = target.strip().replace("\\", "/")
    candidates = [
        item
        for item in attachments
        if item.relative_path
        and (
            item.relative_path.casefold()
            == str(
                PurePosixPath(note_path).parent.joinpath(PurePosixPath(clean))
            ).casefold()
            or item.relative_path.casefold() == clean.casefold()
            or PurePosixPath(item.relative_path).name.casefold()
            == PurePosixPath(clean).name.casefold()
        )
    ]
    unique = {item.item_key: item for item in candidates}
    return next(iter(unique.values())) if len(unique) == 1 else None


def rewrite_local_references(
    content: bytes,
    *,
    note_path: str,
    attachments: Sequence[InventoryItem],
    root_id: str,
    document_id: str,
) -> tuple[bytes, tuple[dict[str, Any], ...]]:
    """Replace accepted local attachment targets with opaque link URIs."""

    text = content.decode("utf-8-sig")
    rewrites: list[dict[str, Any]] = []

    def replacement(item: InventoryItem, target: str) -> str:
        link_id = _local_link_id(root_id, document_id, item)
        rewrites.append(
            {
                "link_id": link_id,
                "original_target": target,
                "relative_path": item.relative_path,
                "sha256": item.content_sha256,
            }
        )
        return f"wb-local-file:{link_id}"

    def wiki(match: re.Match[str]) -> str:
        target, tail = match.group(1), match.group(2)
        item = _resolve_reference(note_path, target, attachments)
        if item is None:
            return match.group(0)
        alias = tail.lstrip("|").strip() or PurePosixPath(target).name
        # Co-work's structured Markdown kernel intentionally escapes Obsidian
        # wikilink syntax.  Convert the accepted embed to ordinary Markdown
        # while replacing its target with the opaque resolver URI.
        return f"Local file ({alias}): {replacement(item, target)}"

    def markdown(match: re.Match[str]) -> str:
        target = match.group(2)
        item = _resolve_reference(note_path, target, attachments)
        if item is None:
            return match.group(0)
        label_match = re.search(r"\[([^\]]*)\]", match.group(1))
        label = label_match.group(1) if label_match else PurePosixPath(target).name
        return f"Local file ({label}): {replacement(item, target)}"

    text = _WIKI_EMBED_RE.sub(wiki, text)
    text = _MARKDOWN_LINK_RE.sub(markdown, text)
    expected = {item.item_key for item in attachments}
    observed = {
        item.item_key
        for item in attachments
        if any(rewrite["relative_path"] == item.relative_path for rewrite in rewrites)
    }
    if expected != observed:
        missing = sorted(expected - observed)
        raise LegacyInventoryError(
            "An accepted local reference could not be rewritten unambiguously.",
            details={"note": note_path, "missing": missing},
        )
    encoded = text.encode("utf-8")
    if content.startswith(b"\xef\xbb\xbf"):
        encoded = b"\xef\xbb\xbf" + encoded
    return encoded, tuple(sorted(rewrites, key=lambda item: item["link_id"]))


@dataclass(frozen=True, slots=True)
class ImportedLegacyDocument:
    note_uuid: str
    task_id: str | None
    store_id: str
    document_id: str
    binding_id: str | None
    source_ref: str
    source_receipt_id: str
    source_content_sha256: str
    normalized_content_sha256: str
    document_content_sha256: str
    document_head_sha256: str
    lifecycle: str
    classification: str
    byte_parity: bool
    normalized_parity: bool
    projection_strategy: str
    rewrites: tuple[Mapping[str, Any], ...]


class LegacyTaskDocumentImporter:
    """Capture exact Markdown Sources and create inert task Co-work shadows."""

    def __init__(
        self,
        *,
        source_root: str | Path,
        sources: SourceStore,
        principal: ActorRef,
        stores: TaskDocumentStoreManager,
        kernel: DocumentKernelClient | None = None,
        attestation_actor_ref: str = "legacy-task-migration-operator",
    ) -> None:
        self.source_root = Path(source_root).expanduser().resolve()
        self.sources = sources
        self.principal = principal
        self.stores = stores
        self.attestation_actor_ref = str(attestation_actor_ref).strip()
        if not self.attestation_actor_ref:
            raise ValueError("attestation_actor_ref is required")
        self.kernel = kernel or DocumentKernelClient()
        self._owns_kernel = kernel is None
        self.provider = WorkBuddyFileImportProvider(
            self.source_root,
            tenant_scope_id=principal.tenant_scope_id,
        )
        self.providers = ProviderRegistry()
        self.providers.register(self.provider)

    def close(self) -> None:
        if self._owns_kernel:
            self.kernel.close()

    def __enter__(self) -> "LegacyTaskDocumentImporter":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _bootstrap_projection(
        self,
        transformed: bytes,
        *,
        request_id: str,
    ) -> tuple[Any, str]:
        def request(source: bytes, suffix: str = "") -> Any:
            return self.kernel.request(
                {
                    "kind": "bootstrap_markdown",
                    "sourceBase64": source,
                    "sourceSha256": sha256_bytes(source),
                    "newlineStyle": _newline_style(source),
                    "utf8Bom": source.startswith(b"\xef\xbb\xbf"),
                    "trailingNewlineCount": _trailing_newlines(source),
                },
                request_id=request_id + suffix,
            )

        outcome = request(transformed)
        if outcome.snapshot is not None and outcome.projection is not None:
            if _kernel_projection_equivalent(outcome.projection, transformed):
                return outcome, "structured_markdown"

        literal = request(_literal_markdown_envelope(transformed), "_literal")
        if literal.snapshot is None or literal.projection is None:
            raise LegacyInventoryError("Document kernel returned no task-note result.")
        if not _literal_projection_equivalent(literal.projection, transformed):
            raise LegacyInventoryError(
                "Document kernel could not preserve the legacy note even in literal mode."
            )
        return literal, "literal_markdown_fallback"

    def import_note(
        self,
        item: InventoryItem,
        *,
        attachments: Sequence[InventoryItem] = (),
        root_id: str,
    ) -> ImportedLegacyDocument:
        if (
            item.classification not in _IMPORTABLE_DOCUMENT_CLASSES
            or not item.relative_path
            or not item.note_uuid
            or not item.content_sha256
        ):
            raise LegacyInventoryError("Only accepted UUID task notes can be imported.")
        source_ref = source_capture_from_origin(
            self.sources,
            self.providers,
            provider_id=self.provider.provider_id,
            origin_ref=OriginRef(
                provider_id=self.provider.provider_id,
                container_id=self.provider.container_id,
                native_item_id=item.relative_path,
                revision=item.content_sha256,
            ),
            principal=self.principal,
            purpose="file_import",
            tenant_scope_id=self.principal.tenant_scope_id,
            originating_surface="legacy_task_shadow_import",
            expected_revision=item.content_sha256,
            expected_digest=item.content_sha256,
            namespace="legacy-task-cohort",
        )
        source_item = self.sources.get_item(source_ref)
        if source_item is None:
            raise LegacyInventoryError("Captured task-note Source is unavailable.")
        representation = self.sources.get_representation(
            source_item.primary_representation_id
        )
        if representation is None:
            raise LegacyInventoryError("Captured task-note representation is unavailable.")
        document_id = _document_id(item.note_uuid)
        reserved = resolve_and_reserve_source(
            self.sources,
            source_ref=source_ref,
            representation_id=representation.representation_id,
            principal=self.principal,
            purpose="file_import",
            consumer_domain="cowork_document",
            consumer_id=document_id,
            use_kind="exact_insertion",
            disclosure_kind="exact_readable_copy",
            redaction_policy="scrub",
            selector={"kind": "whole_document/v1"},
            expected_digest=item.content_sha256,
        )
        exact = reserved.resolved.content
        transformed, rewrites = rewrite_local_references(
            exact,
            note_path=item.relative_path,
            attachments=attachments,
            root_id=root_id,
            document_id=document_id,
        )
        store = self.stores.ensure()
        projection_strategy = "structured_markdown"
        try:
            document = documents.get_document(store, document_id)
            projection = store.resolve_blob_path(
                f"blobs/{document.content_sha256}"
            ).read_bytes()
            if document.meta_json:
                try:
                    document_meta = json.loads(document.meta_json)
                except json.JSONDecodeError:
                    document_meta = {}
                projection_strategy = str(
                    document_meta.get("legacy_projection_strategy")
                    or projection_strategy
                )
            if not _projection_matches_strategy(
                projection,
                transformed,
                strategy=projection_strategy,
            ):
                raise LegacyInventoryError(
                    "A deterministic imported document already exists with different content.",
                    details={"note_uuid": item.note_uuid},
                )
        except InvariantViolation:
            outcome, projection_strategy = self._bootstrap_projection(
                transformed,
                request_id=f"legacy_task_import_{document_id}",
            )
            assert outcome.snapshot is not None and outcome.projection is not None
            snapshot_sha = ydoc_store.write_snapshot(
                store,
                snapshot=outcome.snapshot,
                expected_sha256=sha256_bytes(outcome.snapshot),
            )
            document, _version, _created = documents.register_ready_document(
                store,
                path=_document_path(item.note_uuid),
                title=_title(exact, item.note_uuid),
                document_class="co_authored",
                projection_bytes=outcome.projection,
                ydoc_snapshot_sha256=snapshot_sha,
                structured_head_sha256=structured_head_sha256(outcome.snapshot),
                actor=Actor(kind="system", ref="legacy-task-shadow-import"),
                mode="import",
                document_meta={
                    "domain_content": True,
                    "domain_namespace": "tasks",
                    "migration_read_only": True,
                    "legacy_projection_strategy": projection_strategy,
                    "source": {
                        "kind": "file_import",
                        "source_ref": source_ref.uri,
                        "sha256": item.content_sha256,
                        "writeback_policy": "never",
                        "authorship": "unknown",
                    },
                    "local_link_rewrites": list(rewrites),
                },
                document_id=document_id,
                version_id=hashlib.sha256(
                    f"legacy-task-version/v1\0{item.note_uuid}\0{item.content_sha256}".encode()
                ).hexdigest()[:32],
            )
            projection = outcome.projection

        if document.ydoc_snapshot_sha256 is None:
            raise LegacyInventoryError("Imported document has no structured head.")
        head = ydoc_store.current_structured_head(
            store,
            document_id=document.id,
            snapshot_sha256=document.ydoc_snapshot_sha256,
        )
        binding_id: str | None = None
        lifecycle = "recovery" if item.task_id is None else "current"
        if item.task_id is not None:
            causality = DocumentCausalityStore(store.paths.sidecar)
            expected_binding_id = _binding_id(item.task_id, store.store_id, document.id)
            existing_binding = causality.get_binding(expected_binding_id)
            if existing_binding is not None and existing_binding.lifecycle == "retired":
                if item.classification != "task_note_deleted":
                    raise LegacyInventoryError("A live task resolved to a retired binding.")
                binding = existing_binding
            else:
                binding = causality.ensure_binding(
                    domain_namespace="tasks",
                    domain_kind="task_knowledge",
                    domain_entity_id=item.task_id,
                    domain_revision=item.content_sha256,
                    store_id=store.store_id,
                    document_id=document.id,
                    role="task_knowledge",
                    created_by="service:legacy-task-shadow-import",
                    projection_path=None,
                    projection_mode="none",
                    migration_origin="legacy-task-cohort/v1",
                )
            if item.classification == "task_note_deleted":
                binding = causality.retire_binding(binding.binding_id)
                if documents.current_lifecycle(store, document.id) != "retired":
                    documents.retire_document(
                        store,
                        document_id=document.id,
                        actor=Actor(kind="system", ref="legacy-task-shadow-import"),
                    )
                lifecycle = "retired"
            binding_id = binding.binding_id
        provenance.record_document_attestation(
            store,
            document_id=document.id,
            attestation={
                "schema": provenance.INPUT_ATTESTATION_SCHEMA,
                "authorship": {"kind": "unknown", "contributors": []},
                "human_review": {"status": "not_applicable", "reviewers": []},
            },
            source={
                "kind": "file_import",
                "format": "markdown",
                "media_type": "text/markdown",
                "sha256": item.content_sha256,
                "source_ref": source_ref.uri,
            },
            actor=Actor(kind="human", ref=self.attestation_actor_ref),
            idempotency_key=f"legacy-task-import:{item.note_uuid}:{item.content_sha256}",
            basis_kind="user_attestation",
        )
        if reserved.reservation.status == "reserved":
            self.sources.precommit_recheck_usage(reserved.reservation.usage_id)
        self.sources.acknowledge_usage(reserved.reservation.usage_id)
        self.stores.registry.touch(store)
        expected_document_sha = sha256_bytes(projection)
        byte_parity = sha256_bytes(exact) == item.content_sha256
        normalized_parity = _projection_matches_strategy(
            projection,
            transformed,
            strategy=projection_strategy,
        )
        if not byte_parity or not normalized_parity:
            raise LegacyInventoryError("Imported task document failed byte/normalized parity.")
        return ImportedLegacyDocument(
            note_uuid=item.note_uuid,
            task_id=item.task_id,
            store_id=store.store_id,
            document_id=document.id,
            binding_id=binding_id,
            source_ref=source_ref.uri,
            source_receipt_id=reserved.reservation.usage_id,
            source_content_sha256=item.content_sha256,
            normalized_content_sha256=normalized_markdown_sha256(exact),
            document_content_sha256=expected_document_sha,
            document_head_sha256=head,
            lifecycle=lifecycle,
            classification=item.classification,
            byte_parity=byte_parity,
            normalized_parity=normalized_parity,
            projection_strategy=projection_strategy,
            rewrites=rewrites,
        )


class LegacyTaskCutoverOperator:
    """Orchestrate shadow import and the explicit prepare/apply/activate flow."""

    def __init__(
        self,
        *,
        inventory: LegacyInventory,
        source_root: str | Path,
        task_store: TaskStore,
        document_importer: LegacyTaskDocumentImporter,
        actor: str,
        session_id: str | None,
    ) -> None:
        self.inventory = inventory
        self.source_root = Path(source_root).expanduser().resolve()
        self.task_store = task_store
        self.documents = document_importer
        self.actor = actor
        self.session_id = session_id
        self.ledger = TaskMigrationLedger(task_store)
        self._accepted_inventory_sha256: str | None = None

    def _canonical_inventory_sha256(self) -> str:
        cohort = self.ledger.cohort(self.inventory.cohort_id)
        if cohort is None:
            raise CohortStateError("The migration cohort does not exist.")
        stored = str(cohort["inventory_sha256"])
        if self._accepted_inventory_sha256 is None:
            if stored != self.inventory.inventory_sha256:
                raise CohortStateError(
                    "A rebuilt inventory must be accepted by shadow replay before cutover continues."
                )
            self._accepted_inventory_sha256 = stored
        elif self._accepted_inventory_sha256 != stored:
            raise CohortStateError("The cohort inventory digest changed after replay acceptance.")
        return stored

    @staticmethod
    def _resume_failure(note_uuid: str, reason: str) -> None:
        raise LegacyInventoryError(
            "A staged task document failed fast-resume verification.",
            details={"note_uuid": note_uuid, "reason": reason},
        )

    def _current_note_bytes(self, item: InventoryItem) -> bytes:
        assert item.note_uuid and item.relative_path and item.content_sha256
        path = self.source_root.joinpath(*PurePosixPath(item.relative_path).parts)
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.source_root)
            if path.is_symlink() or not resolved.is_file():
                raise OSError("not a regular source file")
            content = resolved.read_bytes()
        except (OSError, ValueError) as exc:
            self._resume_failure(item.note_uuid, f"source file unavailable: {exc}")
        if (
            len(content) != int(item.byte_length or 0)
            or sha256_bytes(content) != item.content_sha256
        ):
            self._resume_failure(item.note_uuid, "source bytes no longer match the inventory")
        try:
            content.decode("utf-8-sig")
        except UnicodeDecodeError:
            self._resume_failure(item.note_uuid, "source bytes are no longer valid UTF-8")
        return content

    @staticmethod
    def _expected_lifecycle(item: InventoryItem) -> str:
        if item.classification == "task_note_deleted":
            return "retired"
        return "recovery" if item.task_id is None else "current"

    @staticmethod
    def _expected_local_link_rows(
        *,
        result: ImportedLegacyDocument,
        attachments: Sequence[InventoryItem],
        root_id: str,
    ) -> dict[str, dict[str, Any]]:
        attachment_by_path = {
            str(attachment.relative_path): attachment
            for attachment in attachments
            if attachment.relative_path
        }
        expected: dict[str, dict[str, Any]] = {}
        for rewrite in result.rewrites:
            relative_path = str(rewrite["relative_path"])
            attachment = attachment_by_path.get(relative_path)
            if attachment is None or not attachment.content_sha256:
                raise LegacyInventoryError(
                    "A rewritten local reference has no inventory attachment.",
                    details={
                        "note_uuid": result.note_uuid,
                        "relative_path": relative_path,
                    },
                )
            suffix = PurePosixPath(relative_path).suffix.casefold()
            row = {
                "link_id": str(rewrite["link_id"]),
                "task_id": result.task_id,
                "note_uuid": result.note_uuid,
                "store_id": result.store_id,
                "document_id": result.document_id,
                "root_id": root_id,
                "relative_path": relative_path,
                "display_name": PurePosixPath(relative_path).name,
                "suffix": suffix,
                "media_type": mimetypes.guess_type(relative_path)[0]
                or "application/octet-stream",
                "byte_length": int(attachment.byte_length or 0),
                "sha256": str(attachment.content_sha256),
                "sensitivity": "credential" if suffix == ".ppk" else "private",
                "allowed_action": "reveal" if suffix == ".ppk" else "open",
                "policy_revision": 1,
                "source_receipt_id": result.source_receipt_id,
            }
            prior = expected.get(row["link_id"])
            if prior is not None and prior != row:
                raise LegacyInventoryError(
                    "A task note produced a conflicting local-link identity.",
                    details={"note_uuid": result.note_uuid, "link_id": row["link_id"]},
                )
            expected[row["link_id"]] = row
        return expected

    @staticmethod
    def _local_link_row_matches(
        observed: Mapping[str, Any], expected: Mapping[str, Any]
    ) -> bool:
        return observed.get("activated_at") is None and all(
            observed.get(key) == value for key, value in expected.items()
        )

    def _stage_expected_local_links(
        self,
        result: ImportedLegacyDocument,
        *,
        attachments: Sequence[InventoryItem],
        root_id: str,
        observed_rows: Sequence[Mapping[str, Any]] = (),
    ) -> int:
        expected = self._expected_local_link_rows(
            result=result,
            attachments=attachments,
            root_id=root_id,
        )
        observed = {str(row["link_id"]): row for row in observed_rows}
        if len(observed) != len(observed_rows):
            self._resume_failure(result.note_uuid, "duplicate local-link stage identities")
        extra = sorted(set(observed) - set(expected))
        if extra:
            self._resume_failure(
                result.note_uuid,
                f"unexpected local-link stage rows: {', '.join(extra)}",
            )
        for link_id, row in observed.items():
            if not self._local_link_row_matches(row, expected[link_id]):
                self._resume_failure(result.note_uuid, f"local-link stage drift: {link_id}")
        for link_id in sorted(set(expected) - set(observed)):
            self.ledger.stage_local_file_link(
                self.inventory.cohort_id,
                **expected[link_id],
            )
        if set(expected) != set(observed):
            _documents, refreshed = self.ledger.shadow_stage_snapshot(
                self.inventory.cohort_id
            )
            repaired = refreshed.get(result.note_uuid, ())
            repaired_by_id = {str(row["link_id"]): row for row in repaired}
            if set(repaired_by_id) != set(expected) or any(
                not self._local_link_row_matches(repaired_by_id[link_id], row)
                for link_id, row in expected.items()
            ):
                self._resume_failure(
                    result.note_uuid, "local-link stage repair was not exact"
                )
        if expected:
            conn = self.task_store.connect()
            try:
                root = conn.execute(
                    "SELECT * FROM task_local_file_roots WHERE root_id=?",
                    (root_id,),
                ).fetchone()
                sealed_gate = conn.execute(
                    "SELECT passed FROM task_migration_gates "
                    "WHERE cohort_id=? AND gate_name='frozen_tree_sealed'",
                    (self.inventory.cohort_id,),
                ).fetchone()
            finally:
                conn.close()
            expected_status = (
                "sealed" if sealed_gate is not None and bool(sealed_gate[0]) else "pending_seal"
            )
            if (
                root is None
                or str(root["label"]) != "Frozen legacy task tree"
                or str(root["manifest_sha256"]) != self.inventory.manifest_sha256
                or int(root["policy_revision"]) != 1
                or str(root["status"]) != expected_status
            ):
                self._resume_failure(result.note_uuid, "local-file root changed")
        return len(expected)

    def _verify_source_receipt(
        self,
        *,
        item: InventoryItem,
        document_id: str,
        source_ref: str,
        source_receipt_id: str,
        expected_content: bytes,
    ) -> None:
        assert item.note_uuid and item.relative_path and item.content_sha256
        conn = self.documents.sources.connect()
        try:
            row = conn.execute(
                """
                SELECT usage.*, representation.content_sha256 AS representation_sha256,
                       representation.byte_length AS representation_bytes,
                       representation.is_primary, representation.redacted_at,
                       source.lifecycle_state, source.redaction_epoch,
                       source.primary_representation_id, source.origin_ref_json,
                       source.native_revision, source.tenant_scope_id,
                       source.originating_surface, source.namespace,
                       access.principal_ref_json AS access_principal_ref_json,
                       access.authority_id AS access_authority_id,
                       access.source_item_id AS access_source_item_id,
                       access.purpose AS access_purpose,
                       access.access_mode, access.trusted_service_id,
                       access.scope_json AS access_scope_json,
                       access.revoked_at AS access_revoked_at
                FROM source_usage_intents AS usage
                JOIN source_representations AS representation
                  ON representation.representation_id=usage.representation_id
                JOIN source_items AS source
                  ON source.authority_id=usage.authority_id
                 AND source.source_item_id=usage.source_item_id
                JOIN source_access_bindings AS access
                  ON access.binding_id=usage.access_binding_id
                WHERE usage.usage_id=?
                """,
                (source_receipt_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            self._resume_failure(item.note_uuid, f"source receipt lookup failed: {exc}")
        finally:
            conn.close()
        if row is None:
            self._resume_failure(item.note_uuid, "source receipt is missing")
        observed_source_ref = (
            f"wb-source://{row['authority_id']}/item/{row['source_item_id']}"
        )
        try:
            selector = json.loads(str(row["selector_json"]))
            origin = json.loads(str(row["origin_ref_json"]))
            principal = json.loads(str(row["principal_ref_json"]))
            access_principal = json.loads(str(row["access_principal_ref_json"]))
            access_scope = json.loads(str(row["access_scope_json"]))
        except (TypeError, json.JSONDecodeError):
            self._resume_failure(item.note_uuid, "source receipt metadata is malformed")
        expected_origin = OriginRef(
            provider_id=self.documents.provider.provider_id,
            container_id=self.documents.provider.container_id,
            native_item_id=item.relative_path,
            revision=item.content_sha256,
        ).to_dict()
        request = {
            "source_ref": SourceRef.parse(source_ref).to_dict(),
            "representation_id": str(row["representation_id"]),
            "principal": self.documents.principal.to_dict(),
            "purpose": "file_import",
            "consumer_domain": "cowork_document",
            "consumer_id": document_id,
            "use_kind": "exact_insertion",
            "disclosure_kind": "exact_readable_copy",
            "redaction_policy": "scrub",
            "selector": {"kind": "whole_document/v1"},
            "external_recipient": None,
            "model_id": None,
            "egress_class": None,
        }
        checks = (
            observed_source_ref == source_ref,
            str(row["authority_id"]) == self.documents.sources.authority_id,
            str(row["status"]) == "acknowledged",
            str(row["maintenance_state"]) == "clean",
            str(row["lifecycle_state"]) == "active",
            int(row["bound_redaction_epoch"]) == int(row["redaction_epoch"]),
            str(row["consumer_domain"]) == "cowork_document",
            str(row["consumer_id"]) == document_id,
            str(row["use_kind"]) == "exact_insertion",
            str(row["purpose"]) == "file_import",
            str(row["disclosure_kind"]) == "exact_readable_copy",
            str(row["redaction_policy"]) == "scrub",
            selector == {"kind": "whole_document/v1"},
            principal == self.documents.principal.to_dict(),
            str(row["request_sha256"]) == canonical_sha256(request),
            access_principal == self.documents.principal.to_dict(),
            str(row["access_authority_id"]) == str(row["authority_id"]),
            str(row["access_source_item_id"]) == str(row["source_item_id"]),
            str(row["access_purpose"]) == "file_import",
            str(row["access_mode"]) == "content",
            str(row["trusted_service_id"]) == self.documents.provider.provider_id,
            access_scope
            == {"tenant_scope_id": self.documents.principal.tenant_scope_id},
            row["access_revoked_at"] is None,
            str(row["representation_sha256"]) == item.content_sha256,
            int(row["representation_bytes"]) == int(item.byte_length or 0),
            bool(row["is_primary"]),
            row["redacted_at"] is None,
            str(row["primary_representation_id"]) == str(row["representation_id"]),
            str(row["native_revision"]) == item.content_sha256,
            str(row["tenant_scope_id"]) == self.documents.principal.tenant_scope_id,
            str(row["originating_surface"]) == "legacy_task_shadow_import",
            str(row["namespace"]) == "legacy-task-cohort",
            origin == expected_origin,
        )
        if not all(checks):
            self._resume_failure(item.note_uuid, "source receipt no longer matches the import")
        try:
            resolved = resolve_source(
                self.documents.sources,
                source_ref=SourceRef.parse(source_ref),
                principal=self.documents.principal,
                purpose="file_import",
                representation_id=str(row["representation_id"]),
                expected_digest=item.content_sha256,
            )
        except SourceError:
            self._resume_failure(item.note_uuid, "retained Source verification failed")
        if resolved.content != expected_content:
            self._resume_failure(item.note_uuid, "retained Source bytes changed")

    def _source_receipt_for_document(self, *, note_uuid: str, document_id: str) -> str:
        conn = self.documents.sources.connect()
        try:
            rows = conn.execute(
                "SELECT usage_id FROM source_usage_intents "
                "WHERE consumer_domain='cowork_document' AND consumer_id=? "
                "AND use_kind='exact_insertion' ORDER BY usage_id",
                (document_id,),
            ).fetchall()
        finally:
            conn.close()
        if len(rows) != 1:
            self._resume_failure(
                note_uuid, "a NULL stage receipt has no unique retained Source usage"
            )
        return str(rows[0]["usage_id"])

    def _verify_binding(
        self,
        *,
        item: InventoryItem,
        store_id: str,
        document_id: str,
        binding_id: str | None,
        lifecycle: str,
        causality: DocumentCausalityStore,
    ) -> None:
        assert item.note_uuid and item.content_sha256
        with causality.connection() as conn:
            document_binding_ids = {
                str(row[0])
                for row in conn.execute(
                    "SELECT binding_id FROM domain_document_bindings "
                    "WHERE store_id=? AND document_id=?",
                    (store_id, document_id),
                )
            }
        if item.task_id is None:
            if binding_id is not None or document_binding_ids:
                self._resume_failure(item.note_uuid, "recovery document has a binding")
            return
        expected_id = _binding_id(item.task_id, store_id, document_id)
        if binding_id != expected_id or document_binding_ids != {expected_id}:
            self._resume_failure(item.note_uuid, "binding identity changed")
        binding = causality.get_binding(expected_id)
        expected_binding_lifecycle = "retired" if lifecycle == "retired" else "current"
        if (
            binding is None
            or binding.domain_namespace != "tasks"
            or binding.domain_kind != "task_knowledge"
            or binding.domain_entity_id != item.task_id
            or binding.domain_revision != item.content_sha256
            or binding.store_id != store_id
            or binding.document_id != document_id
            or binding.role != "task_knowledge"
            or binding.lifecycle != expected_binding_lifecycle
            or binding.content_authority != "domain"
            or binding.content_authority_epoch != 0
            or binding.projection_mode != "none"
            or binding.projection_path is not None
            or binding.migration_origin != "legacy-task-cohort/v1"
        ):
            self._resume_failure(item.note_uuid, "binding state changed")

    def _verify_shadow_catalogs(
        self,
        *,
        item: InventoryItem,
        store_id: str,
        document_id: str,
        source_receipt_id: str,
        lifecycle: str,
    ) -> None:
        assert item.note_uuid
        conn = self.task_store.connect()
        try:
            recovery = conn.execute(
                "SELECT * FROM recovered_task_documents WHERE note_uuid=?",
                (item.note_uuid,),
            ).fetchone()
            task_links = conn.execute(
                "SELECT COUNT(*) FROM task_document_links "
                "WHERE note_uuid=? OR (store_id=? AND document_id=?)",
                (item.note_uuid, store_id, document_id),
            ).fetchone()[0]
            active_local_links = conn.execute(
                "SELECT COUNT(*) FROM task_local_file_links "
                "WHERE store_id=? AND document_id=?",
                (store_id, document_id),
            ).fetchone()[0]
        finally:
            conn.close()
        if task_links or active_local_links:
            self._resume_failure(item.note_uuid, "shadow artifacts were activated early")
        if item.classification != "recovered_task_document":
            if recovery is not None:
                self._resume_failure(item.note_uuid, "unexpected recovery catalog row")
            return
        expected_recovery_id = "recovery_" + hashlib.sha256(
            f"{self.inventory.cohort_id}\0{item.note_uuid}".encode("utf-8")
        ).hexdigest()[:32]
        if (
            recovery is None
            or str(recovery["recovery_id"]) != expected_recovery_id
            or str(recovery["store_id"]) != store_id
            or str(recovery["document_id"]) != document_id
            or str(recovery["source_receipt_id"]) != source_receipt_id
            or str(recovery["classification"]) != item.classification
            or str(recovery["lifecycle"]) != lifecycle
            or recovery["claimed_task_id"] is not None
        ):
            self._resume_failure(item.note_uuid, "recovery catalog row changed")

    def _verify_document_history(
        self,
        *,
        item: InventoryItem,
        store: Any,
        document: Any,
        source_ref: str,
        structured_head: str,
    ) -> None:
        assert item.note_uuid and item.content_sha256
        expected_version_id = hashlib.sha256(
            f"legacy-task-version/v1\0{item.note_uuid}\0{item.content_sha256}".encode(
                "utf-8"
            )
        ).hexdigest()[:32]
        versions = documents.document_versions(store, document.id)
        if len(versions) != 1:
            self._resume_failure(item.note_uuid, "document version history changed")
        version = versions[0]
        if (
            version.id != expected_version_id
            or version.document_id != document.id
            or version.kind != "initial_import"
            or version.projection_sha256 != document.content_sha256
            or version.ydoc_snapshot_sha256 != document.ydoc_snapshot_sha256
            or version.structured_head_sha256 != structured_head
            or version.actor_kind != "system"
            or version.actor_ref != "legacy-task-shadow-import"
            or version.detail != "import"
            or document.created_by_kind != "system"
            or document.created_by_ref != "legacy-task-shadow-import"
        ):
            self._resume_failure(item.note_uuid, "initial document version changed")
        attestations = store.list_document_provenance_attestations(document.id)
        expected_attestation_key = (
            f"legacy-task-import:{item.note_uuid}:{item.content_sha256}"
        )
        matching_attestations = tuple(
            row
            for row in attestations
            if row.idempotency_key == expected_attestation_key
        )
        if len(matching_attestations) != 1:
            self._resume_failure(item.note_uuid, "document provenance history changed")
        attestation = matching_attestations[0]
        compatibility_attestations = tuple(
            row for row in attestations if row is not attestation
        )
        if len(compatibility_attestations) > 1:
            self._resume_failure(item.note_uuid, "document provenance history changed")
        if compatibility_attestations:
            compatibility = compatibility_attestations[0]
            try:
                compatibility_source = json.loads(compatibility.source_json)
                compatibility_contributors = json.loads(
                    compatibility.human_contributors_json
                )
                compatibility_reviewers = json.loads(
                    compatibility.human_reviewers_json
                )
            except (TypeError, json.JSONDecodeError):
                self._resume_failure(
                    item.note_uuid,
                    "document compatibility provenance is malformed",
                )
            expected_compatibility_source = {
                "kind": "file_import",
                "path": document.path,
                "sha256": item.content_sha256,
            }
            expected_compatibility_basis = "truth-schema-v8:legacy-file-import"
            expected_compatibility_id = hashlib.sha256(
                canonical_json(
                    {
                        "schema": provenance.ATTESTATION_SCHEMA,
                        "document_id": document.id,
                        "document_version_id": expected_version_id,
                        "basis_kind": "migration_backfill",
                    }
                ).encode("utf-8")
            ).hexdigest()[:32]
            expected_compatibility_canonical = (
                provenance.attestation_canonical_sha256(
                    document_id=document.id,
                    target_kind="document_version",
                    document_version_id=expected_version_id,
                    document_span_id=None,
                    target_structured_head_sha256=structured_head,
                    authorship_kind="unknown",
                    human_contributors=[],
                    review_status="unknown",
                    human_reviewers=[],
                    source_kind="file_import",
                    source=expected_compatibility_source,
                    basis_kind="migration_backfill",
                    basis_ref=expected_compatibility_basis,
                    supersedes_id=None,
                    attested_by_kind="system",
                    attested_by_ref=None,
                    attested_by_meta=None,
                )
            )
            if (
                compatibility.id != expected_compatibility_id
                or compatibility.document_id != document.id
                or compatibility.target_kind != "document_version"
                or compatibility.document_version_id != expected_version_id
                or compatibility.document_span_id is not None
                or compatibility.target_structured_head_sha256 != structured_head
                or compatibility.authorship_kind != "unknown"
                or compatibility_contributors != []
                or compatibility.review_status != "unknown"
                or compatibility_reviewers != []
                or compatibility.source_kind != "file_import"
                or compatibility_source != expected_compatibility_source
                or compatibility.basis_kind != "migration_backfill"
                or compatibility.basis_ref != expected_compatibility_basis
                or compatibility.supersedes_id is not None
                or compatibility.idempotency_key
                != f"migration:v8:file-import:{expected_version_id}"
                or compatibility.canonical_sha256
                != expected_compatibility_canonical
                or compatibility.created_at != version.created_at
                or compatibility.attested_by_kind != "system"
                or compatibility.attested_by_ref is not None
                or compatibility.attested_by_meta_json is not None
            ):
                self._resume_failure(
                    item.note_uuid,
                    "document compatibility provenance changed",
                )
        try:
            source = json.loads(attestation.source_json)
            contributors = json.loads(attestation.human_contributors_json)
            reviewers = json.loads(attestation.human_reviewers_json)
        except (TypeError, json.JSONDecodeError):
            self._resume_failure(item.note_uuid, "document provenance is malformed")
        expected_source = {
            "kind": "file_import",
            "format": "markdown",
            "media_type": "text/markdown",
            "sha256": item.content_sha256,
            "source_ref": source_ref,
        }
        if (
            attestation.document_id != document.id
            or attestation.target_kind != "document_version"
            or attestation.document_version_id != expected_version_id
            or attestation.document_span_id is not None
            or attestation.target_structured_head_sha256 != structured_head
            or attestation.authorship_kind != "unknown"
            or contributors != []
            or attestation.review_status != "not_applicable"
            or reviewers != []
            or attestation.source_kind != "file_import"
            or source != expected_source
            or attestation.basis_kind != "user_attestation"
            or attestation.basis_ref is not None
            or attestation.supersedes_id is not None
            or attestation.idempotency_key != expected_attestation_key
            or attestation.attested_by_kind != "human"
            or attestation.attested_by_ref != self.documents.attestation_actor_ref
            or attestation.attested_by_meta_json is not None
        ):
            self._resume_failure(item.note_uuid, "document provenance changed")

    def _resume_staged_document(
        self,
        *,
        item: InventoryItem,
        attachments: Sequence[InventoryItem],
        root_id: str,
        staged: Mapping[str, Any],
        staged_links: Sequence[Mapping[str, Any]],
    ) -> tuple[ImportedLegacyDocument, int] | None:
        assert item.note_uuid and item.relative_path and item.content_sha256
        receipt_needs_backfill = staged.get("source_receipt_id") is None
        source_receipt_id = (
            self._source_receipt_for_document(
                note_uuid=item.note_uuid,
                document_id=_document_id(item.note_uuid),
            )
            if receipt_needs_backfill
            else str(staged["source_receipt_id"]).strip()
        )
        if not source_receipt_id:
            self._resume_failure(item.note_uuid, "source receipt is empty")
        exact = self._current_note_bytes(item)
        transformed, rewrites = rewrite_local_references(
            exact,
            note_path=item.relative_path,
            attachments=attachments,
            root_id=root_id,
            document_id=_document_id(item.note_uuid),
        )
        expected_document_id = _document_id(item.note_uuid)
        expected_lifecycle = self._expected_lifecycle(item)
        expected_normalized = normalized_markdown_sha256(exact)
        expected_rewrite_json = canonical_json(list(rewrites))
        expected_basic = {
            "note_uuid": item.note_uuid,
            "task_id": item.task_id,
            "document_id": expected_document_id,
            "source_content_sha256": item.content_sha256,
            "normalized_content_sha256": expected_normalized,
            "lifecycle": expected_lifecycle,
            "classification": item.classification,
        }
        if any(staged.get(key) != value for key, value in expected_basic.items()):
            self._resume_failure(item.note_uuid, "document stage no longer matches inventory")
        if not bool(staged.get("byte_parity")) or not bool(
            staged.get("normalized_parity")
        ):
            self._resume_failure(item.note_uuid, "document parity stage is incomplete")
        if staged.get("activated_at") is not None:
            self._resume_failure(item.note_uuid, "document stage is already activated")
        try:
            staged_rewrites = canonical_json(
                json.loads(str(staged["rewrite_manifest_json"]))
            )
        except (TypeError, json.JSONDecodeError):
            self._resume_failure(item.note_uuid, "rewrite manifest is malformed")
        if staged_rewrites != expected_rewrite_json:
            self._resume_failure(item.note_uuid, "rewrite manifest changed")

        try:
            store = self.documents.stores.open_existing()
        except (OSError, RuntimeError) as exc:
            self._resume_failure(item.note_uuid, f"task Co-work store unavailable: {exc}")
        if str(staged["store_id"]) != store.store_id:
            self._resume_failure(item.note_uuid, "task Co-work store identity changed")
        try:
            document = documents.get_document(store, expected_document_id)
            projection_path = store.resolve_blob_path(f"blobs/{document.content_sha256}")
            projection = projection_path.read_bytes()
            document_meta = json.loads(document.meta_json or "")
        except (InvariantViolation, OSError, TypeError, json.JSONDecodeError) as exc:
            self._resume_failure(item.note_uuid, f"task Co-work document unavailable: {exc}")
        if not isinstance(document_meta, Mapping):
            self._resume_failure(item.note_uuid, "task Co-work document metadata is malformed")
        projection_strategy = str(document_meta.get("legacy_projection_strategy") or "")
        if projection_strategy != "structured_markdown" and not projection_strategy.startswith(
            "literal_markdown_fallback"
        ):
            self._resume_failure(item.note_uuid, "projection strategy is missing or invalid")
        source_meta = document_meta.get("source")
        if not isinstance(source_meta, Mapping):
            self._resume_failure(item.note_uuid, "document source metadata is missing")
        if (
            document.path != _document_path(item.note_uuid)
            or document.title != _title(exact, item.note_uuid)
            or document.document_class != "co_authored"
            or document.content_sha256 != str(staged["document_content_sha256"])
            or sha256_bytes(projection) != document.content_sha256
            or document.ydoc_snapshot_sha256 is None
            or canonical_json(document_meta.get("local_link_rewrites", []))
            != expected_rewrite_json
            or source_meta.get("kind") != "file_import"
            or source_meta.get("source_ref") != staged["source_ref"]
            or source_meta.get("sha256") != item.content_sha256
            or source_meta.get("writeback_policy") != "never"
            or source_meta.get("authorship") != "unknown"
            or document_meta.get("domain_content") is not True
            or document_meta.get("domain_namespace") != "tasks"
            or document_meta.get("migration_read_only") is not True
            or not _projection_matches_strategy(
                projection, transformed, strategy=projection_strategy
            )
        ):
            self._resume_failure(item.note_uuid, "task Co-work projection changed")
        try:
            current_head = ydoc_store.current_structured_head(
                store,
                document_id=document.id,
                snapshot_sha256=document.ydoc_snapshot_sha256,
            )
            document_lifecycle = documents.current_lifecycle(store, document.id)
        except (InvariantViolation, OSError) as exc:
            self._resume_failure(item.note_uuid, f"structured document head unavailable: {exc}")
        if current_head != str(staged["document_head_sha256"]):
            self._resume_failure(item.note_uuid, "structured document head changed")
        expected_document_lifecycle = (
            "retired" if expected_lifecycle == "retired" else "active"
        )
        if document_lifecycle != expected_document_lifecycle:
            self._resume_failure(item.note_uuid, "document lifecycle changed")
        self._verify_document_history(
            item=item,
            store=store,
            document=document,
            source_ref=str(staged["source_ref"]),
            structured_head=current_head,
        )

        self._verify_source_receipt(
            item=item,
            document_id=expected_document_id,
            source_ref=str(staged["source_ref"]),
            source_receipt_id=source_receipt_id,
            expected_content=exact,
        )
        self._verify_binding(
            item=item,
            store_id=store.store_id,
            document_id=expected_document_id,
            binding_id=(
                None if staged.get("binding_id") is None else str(staged["binding_id"])
            ),
            lifecycle=expected_lifecycle,
            causality=DocumentCausalityStore(store.paths.sidecar),
        )
        self._verify_shadow_catalogs(
            item=item,
            store_id=store.store_id,
            document_id=expected_document_id,
            source_receipt_id=source_receipt_id,
            lifecycle=expected_lifecycle,
        )
        if receipt_needs_backfill:
            self.ledger.backfill_document_stage_source_receipt(
                self.inventory.cohort_id,
                note_uuid=item.note_uuid,
                source_receipt_id=source_receipt_id,
            )
        result = ImportedLegacyDocument(
            note_uuid=item.note_uuid,
            task_id=item.task_id,
            store_id=store.store_id,
            document_id=expected_document_id,
            binding_id=(
                None if staged.get("binding_id") is None else str(staged["binding_id"])
            ),
            source_ref=str(staged["source_ref"]),
            source_receipt_id=source_receipt_id,
            source_content_sha256=item.content_sha256,
            normalized_content_sha256=expected_normalized,
            document_content_sha256=document.content_sha256,
            document_head_sha256=current_head,
            lifecycle=expected_lifecycle,
            classification=item.classification,
            byte_parity=True,
            normalized_parity=True,
            projection_strategy=projection_strategy,
            rewrites=rewrites,
        )
        link_count = self._stage_expected_local_links(
            result,
            attachments=attachments,
            root_id=root_id,
            observed_rows=staged_links,
        )
        return result, link_count

    def _verify_final_shadow_stage(
        self,
        completed: Mapping[
            str, tuple[ImportedLegacyDocument, Sequence[InventoryItem]]
        ],
        *,
        root_id: str,
    ) -> None:
        documents_by_note, links_by_note = self.ledger.shadow_stage_snapshot(
            self.inventory.cohort_id
        )
        if set(documents_by_note) != set(completed) or set(links_by_note) - set(
            completed
        ):
            self._resume_failure(
                next(iter(sorted(set(documents_by_note) ^ set(completed))), "cohort"),
                "final document-stage cohort is not exact",
            )
        for note_uuid, (result, attachments) in completed.items():
            observed = documents_by_note[note_uuid]
            try:
                observed_rewrites = canonical_json(
                    json.loads(str(observed["rewrite_manifest_json"]))
                )
            except (TypeError, json.JSONDecodeError):
                self._resume_failure(note_uuid, "final rewrite manifest is malformed")
            expected_document = {
                "note_uuid": result.note_uuid,
                "task_id": result.task_id,
                "store_id": result.store_id,
                "document_id": result.document_id,
                "binding_id": result.binding_id,
                "source_ref": result.source_ref,
                "source_content_sha256": result.source_content_sha256,
                "normalized_content_sha256": result.normalized_content_sha256,
                "document_content_sha256": result.document_content_sha256,
                "document_head_sha256": result.document_head_sha256,
                "lifecycle": result.lifecycle,
                "classification": result.classification,
                "source_receipt_id": result.source_receipt_id,
            }
            if (
                any(observed.get(key) != value for key, value in expected_document.items())
                or not bool(observed.get("byte_parity"))
                or not bool(observed.get("normalized_parity"))
                or observed.get("activated_at") is not None
                or observed_rewrites != canonical_json(list(result.rewrites))
            ):
                self._resume_failure(note_uuid, "final document-stage row changed")
            expected_links = self._expected_local_link_rows(
                result=result,
                attachments=attachments,
                root_id=root_id,
            )
            observed_links = {
                str(row["link_id"]): row for row in links_by_note.get(note_uuid, ())
            }
            if set(observed_links) != set(expected_links) or any(
                not self._local_link_row_matches(observed_links[link_id], row)
                for link_id, row in expected_links.items()
            ):
                self._resume_failure(note_uuid, "final local-link stage changed")

    def dry_run(self) -> dict[str, Any]:
        """Return the complete scrubbed inventory without writing anything."""
        return self.inventory.to_dict(include_items=True)

    def shadow_import(
        self,
        *,
        backup_receipts: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        self.inventory.require_valid()
        cohort = self.ledger.begin_shadow(
            self.inventory,
            actor=self.actor,
            session_id=self.session_id,
            backup_receipts=backup_receipts,
        )
        if str(cohort["state"]) != "shadow":
            raise CohortStateError(
                "Shadow import replay is only valid while the cohort remains in shadow state."
            )
        self._accepted_inventory_sha256 = str(cohort["inventory_sha256"])
        root_id = _local_root_id(self.inventory.manifest_sha256)
        staged_documents, staged_links = self.ledger.shadow_stage_snapshot(
            self.inventory.cohort_id
        )
        local_items = [
            item for item in self.inventory.items if item.classification in _LOCAL_FILE_CLASSES
        ]
        expected_notes = {
            str(item.note_uuid)
            for item in self.inventory.items
            if item.classification in _IMPORTABLE_DOCUMENT_CLASSES and item.note_uuid
        }
        unexpected_staged_notes = sorted(
            (set(staged_documents) | set(staged_links)) - expected_notes
        )
        if unexpected_staged_notes:
            self._resume_failure(
                unexpected_staged_notes[0],
                "staging contains a note outside the accepted inventory cohort",
            )
        imported = 0
        recovered = 0
        links = 0
        literal_fallbacks = 0
        completed: dict[
            str, tuple[ImportedLegacyDocument, Sequence[InventoryItem]]
        ] = {}
        for item in self.inventory.items:
            if item.classification not in _IMPORTABLE_DOCUMENT_CLASSES:
                continue
            attachments = [
                attachment
                for attachment in local_items
                if item.relative_path
                and item.relative_path in attachment.metadata.get("referenced_by", [])
            ]
            staged = staged_documents.get(str(item.note_uuid))
            if staged is None and staged_links.get(str(item.note_uuid)):
                self._resume_failure(
                    str(item.note_uuid),
                    "local-link rows exist without a document stage row",
                )
            resumed = (
                None
                if staged is None
                else self._resume_staged_document(
                    item=item,
                    attachments=attachments,
                    root_id=root_id,
                    staged=staged,
                    staged_links=staged_links.get(str(item.note_uuid), ()),
                )
            )
            if resumed is None:
                result = self.documents.import_note(
                    item,
                    attachments=attachments,
                    root_id=root_id,
                )
                self.ledger.record_document_stage(
                    self.inventory.cohort_id,
                    note_uuid=result.note_uuid,
                    task_id=result.task_id,
                    store_id=result.store_id,
                    document_id=result.document_id,
                    binding_id=result.binding_id,
                    source_ref=result.source_ref,
                    source_content_sha256=result.source_content_sha256,
                    normalized_content_sha256=result.normalized_content_sha256,
                    document_content_sha256=result.document_content_sha256,
                    document_head_sha256=result.document_head_sha256,
                    rewrite_manifest=result.rewrites,
                    lifecycle=result.lifecycle,
                    classification=result.classification,
                    byte_parity=result.byte_parity,
                    normalized_parity=result.normalized_parity,
                    source_receipt_id=result.source_receipt_id,
                )
                link_count = self._stage_expected_local_links(
                    result,
                    attachments=attachments,
                    root_id=root_id,
                    observed_rows=staged_links.get(str(item.note_uuid), ()),
                )
            else:
                result, link_count = resumed
            links += link_count
            imported += 1
            recovered += int(result.classification == "recovered_task_document")
            literal_fallbacks += int(
                result.projection_strategy.startswith("literal_markdown_fallback")
            )
            completed[result.note_uuid] = (result, attachments)
        self._verify_final_shadow_stage(completed, root_id=root_id)
        cohort = self.ledger.cohort(self.inventory.cohort_id) or cohort
        return {
            "status": "shadow",
            "cohort_id": self.inventory.cohort_id,
            "inventory_sha256": self._accepted_inventory_sha256,
            "documents_imported": imported,
            "recovered_documents": recovered,
            "local_links_staged": links,
            "literal_fallback_documents": literal_fallbacks,
            "cohort": cohort,
        }

    def causality(self) -> DocumentCausalityStore:
        store = self.documents.stores.open_existing()
        return DocumentCausalityStore(store.paths.sidecar)

    def prepare(
        self,
        *,
        target_authority_epoch: str,
        approved_exceptions: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        return self.ledger.prepare(
            self.inventory.cohort_id,
            inventory_sha256=self._canonical_inventory_sha256(),
            target_authority_epoch=target_authority_epoch,
            approved_exceptions=approved_exceptions,
        )

    def apply_and_verify_bindings(self) -> dict[str, int]:
        causality = self.causality()
        applied = self.ledger.apply_bindings(
            self.inventory.cohort_id,
            causality=causality,
        )
        verified = self.ledger.verify_bindings(
            self.inventory.cohort_id,
            causality=causality,
        )
        return {"applied": applied, "verified": verified}

    def activate(
        self,
        *,
        confirmation: str,
        sealed_tree_manifest_sha256: str,
    ) -> dict[str, Any]:
        if confirmation != ACTIVATION_CONFIRMATION:
            raise CutoverPreconditionError(
                "Native task activation needs the exact operator confirmation token."
            )
        # Re-run the exact binding/document-head cohort check immediately
        # before the task-DB activation transaction.  With process generations
        # stopped and the mutation fence armed, this is the final cross-store
        # compare-and-swap receipt rather than a stale earlier observation.
        cohort = self.ledger.cohort(self.inventory.cohort_id)
        if cohort is not None and cohort["state"] != "active":
            self.ledger.verify_bindings(
                self.inventory.cohort_id,
                causality=self.causality(),
            )
        return self.ledger.activate(
            self.inventory.cohort_id,
            inventory_sha256=self._canonical_inventory_sha256(),
            sealed_tree_manifest_sha256=sealed_tree_manifest_sha256,
            actor=self.actor,
            session_id=self.session_id,
        )

    def abort_before_activation(self) -> dict[str, Any]:
        return self.ledger.abort_before_activation(
            self.inventory.cohort_id,
            causality=self.causality(),
            actor=self.actor,
            session_id=self.session_id,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inventory or shadow-import a manifest-fenced legacy task tree."
    )
    parser.add_argument("--cohort-id", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--task-db", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--apply-shadow",
        action="store_true",
        help="Persist the shadow cohort. Without this flag the command is read-only.",
    )
    parser.add_argument("--sources-root")
    parser.add_argument("--cowork-store-root")
    parser.add_argument("--truth-registry")
    parser.add_argument("--actor", default="operator:legacy-task-migration")
    parser.add_argument("--session-id")
    parser.add_argument(
        "--backup-receipts-json",
        help="JSON file containing a list of verified backup receipts (required for shadow).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = LegacyManifestEntry.from_csv(args.manifest)
    inventory = LegacyTaskInventoryBuilder(
        cohort_id=args.cohort_id,
        source_root=args.source_root,
        task_db_path=args.task_db,
        manifest=manifest,
    ).build()
    if not args.apply_shadow:
        print(json.dumps(inventory.to_dict(include_items=True), indent=2, ensure_ascii=False))
        return 0 if inventory.valid else 2
    missing = [
        name
        for name in (
            "sources_root",
            "cowork_store_root",
            "truth_registry",
            "backup_receipts_json",
        )
        if not getattr(args, name)
    ]
    if missing:
        rendered = ", ".join("--" + name.replace("_", "-") for name in missing)
        raise SystemExit("--apply-shadow also requires: " + rendered)
    inventory.require_valid()
    backup_receipts = json.loads(
        Path(args.backup_receipts_json).read_text(encoding="utf-8")
    )
    if not isinstance(backup_receipts, list):
        raise SystemExit("--backup-receipts-json must contain a JSON list")
    if not backup_receipts or any(
        not isinstance(receipt, Mapping) or receipt.get("verified") is not True
        for receipt in backup_receipts
    ):
        raise SystemExit(
            "--apply-shadow requires at least one verified backup receipt"
        )
    task_store = TaskStore(args.task_db)
    task_store.initialize()
    sources = SourceStore.create(args.sources_root)
    principal = ActorRef(
        sources.authority_id,
        "legacy-task-migration",
        "service",
        "task-migration-tenant",
    )
    stores = TaskDocumentStoreManager(
        root=args.cowork_store_root,
        registry=TruthStoreRegistry(args.truth_registry),
    )
    with LegacyTaskDocumentImporter(
        source_root=args.source_root,
        sources=sources,
        principal=principal,
        stores=stores,
        attestation_actor_ref=args.actor,
    ) as importer:
        result = LegacyTaskCutoverOperator(
            inventory=inventory,
            source_root=args.source_root,
            task_store=task_store,
            document_importer=importer,
            actor=args.actor,
            session_id=args.session_id,
        ).shadow_import(backup_receipts=backup_receipts)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through CLI smoke tests
    raise SystemExit(main())


__all__ = [
    "ACTIVATION_CONFIRMATION",
    "ImportedLegacyDocument",
    "LegacyTaskCutoverOperator",
    "LegacyTaskDocumentImporter",
    "main",
    "rewrite_local_references",
]
