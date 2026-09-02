"""Native Co-work knowledge documents owned by the task domain.

This module deliberately has no vault, Obsidian, Tasks-plugin, or filesystem
projection dependency.  The only files it owns are the internal Truth-store
database and content-addressed Yjs blobs below the configured Work Buddy data
root.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from work_buddy.document_kernel.causality import (
    DocumentCausalityStore,
    DomainDocumentBinding,
)
from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.document_kernel.protocol import sha256_bytes, structured_head_sha256
from work_buddy.paths import resolve
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.registry import TruthRegistryError, TruthStoreRegistry
from work_buddy.truth.store import TruthStore

from .errors import TaskAuthorityUnavailable
from .store import default_task_db_path


_STORE_LOCK = threading.RLock()
_TASK_STORE_ID = hashlib.sha256(b"work-buddy-task-knowledge-store/v1").hexdigest()[:32]


def _stable_id(kind: str, task_id: str) -> str:
    return hashlib.sha256(
        f"work-buddy-task-knowledge/{kind}/v1\0{task_id}".encode("utf-8")
    ).hexdigest()[:32]


def project_live_markdown(
    store: TruthStore,
    document_id: str,
    *,
    kernel: DocumentKernelClient | None = None,
) -> str:
    """Project the current Y.Doc snapshot plus every un-compacted update."""

    document = documents.get_document(store, document_id)
    if document.ydoc_snapshot_sha256 is None:
        raise RuntimeError("task_document_snapshot_unavailable")
    snapshot = ydoc_store.read_snapshot(
        store,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    updates, _cursor = ydoc_store.read_updates(store, document_id=document.id)
    head = structured_head_sha256(snapshot, updates)
    client = kernel or DocumentKernelClient()
    projected = client.request(
        {
            "kind": "project_markdown",
            "snapshotBase64": snapshot,
            "updatesBase64": updates,
            "expectedBaseStructuredHeadSha256": head,
        },
        request_id=f"task_read_{hashlib.sha256(f'{document.id}:{head}'.encode()).hexdigest()[:24]}",
    )
    if projected.projection is None:
        raise RuntimeError("document_kernel_missing_projection")
    return projected.projection.decode("utf-8")


@dataclass(frozen=True, slots=True)
class TaskKnowledgeDocument:
    task_id: str
    store_id: str
    document_id: str
    binding_id: str
    title: str
    lifecycle: str

    @property
    def href(self) -> str:
        return (
            "/app/cowork?store_id="
            f"{self.store_id}&document_id={self.document_id}"
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "store_id": self.store_id,
            "document_id": self.document_id,
            "binding_id": self.binding_id,
            "title": self.title,
            "lifecycle": self.lifecycle,
            "href": self.href,
        }


class TaskDocumentStoreManager:
    """Open the single stable task-owned Co-work store."""

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        registry: TruthStoreRegistry | None = None,
    ) -> None:
        self._explicit_root = root is not None
        self.root = (
            Path(root).expanduser().resolve()
            if root is not None
            else resolve("stores/cowork-tasks").resolve()
        )
        self.registry = registry or TruthStoreRegistry()

    def _authority_store_id(self) -> str | None:
        """Read the cutover-pinned store identity without migrating task state."""

        if self._explicit_root:
            return None
        task_db = default_task_db_path()
        if not task_db.is_file():
            return None
        try:
            uri = task_db.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=2)
            try:
                row = conn.execute(
                    "SELECT cowork_task_store_id FROM task_system_state WHERE id = 1"
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            if "no such table" in str(exc).casefold():
                return None
            raise TaskAuthorityUnavailable() from exc
        except OSError as exc:
            raise TaskAuthorityUnavailable() from exc
        if row is None:
            raise TaskAuthorityUnavailable()
        return str(row[0]).strip() if row[0] else None

    @staticmethod
    def _validate(store: TruthStore, expected_store_id: str) -> TruthStore:
        if store.store_id != expected_store_id:
            raise RuntimeError("task_document_store_identity_mismatch")
        if store.profile.profile != "cowork-task-knowledge-v1":
            raise RuntimeError("task_document_store_profile_mismatch")
        return store

    def _open_pinned(self, store_id: str) -> TruthStore | None:
        """Prefer the registry so a safely relocated task store remains usable."""

        try:
            return self._validate(self.registry.open_store(store_id), store_id)
        except TruthRegistryError:
            return None

    @property
    def sidecar(self) -> Path:
        return self.root / ".wbuddy" / "cowork"

    def ensure(self) -> TruthStore:
        with _STORE_LOCK:
            pinned_store_id = self._authority_store_id()
            if pinned_store_id is not None:
                registered = self._open_pinned(pinned_store_id)
                if registered is not None:
                    return registered
            if (self.sidecar / "store.yaml").is_file():
                store = TruthStore.open(self.sidecar)
                expected_store_id = pinned_store_id or _TASK_STORE_ID
            else:
                if pinned_store_id is not None:
                    raise FileNotFoundError("task_document_store_missing")
                from work_buddy.backups.source_foundation_restore import (
                    require_source_foundation_writable,
                )

                require_source_foundation_writable("tasks.document_store.create")
                self.sidecar.parent.mkdir(parents=True, exist_ok=True)
                store = TruthStore.create(
                    self.sidecar,
                    {
                        "store_id": _TASK_STORE_ID,
                        "profile": "cowork-task-knowledge-v1",
                        "title": "Task knowledge",
                        "allowed_claim_kinds": [
                            "fact",
                            "preference",
                            "decision",
                            "commitment",
                        ],
                        "required_fields": {},
                        "gate": {
                            "rejected_content": "retain",
                            "confirmation_surfaces": [
                                "dashboard",
                                "cli",
                                "chat_consent",
                            ],
                            "block_materialize_on_flags": False,
                        },
                        "projection": "resident",
                        "export_committed": True,
                        "document_surface": {
                            "enabled": True,
                            "allowed_document_classes": ["co_authored"],
                            "feedback_capture": True,
                        },
                    },
                )
                expected_store_id = _TASK_STORE_ID
            self._validate(store, expected_store_id)
            self.registry.register(store)
            return store

    def open_existing(self) -> TruthStore:
        pinned_store_id = self._authority_store_id()
        if pinned_store_id is not None:
            registered = self._open_pinned(pinned_store_id)
            if registered is not None:
                return registered
        if not (self.sidecar / "store.yaml").is_file():
            raise FileNotFoundError("task_document_store_missing")
        store = TruthStore.open(self.sidecar)
        return self._validate(store, pinned_store_id or _TASK_STORE_ID)


class TaskDocumentService:
    """Create and resolve projection-free task knowledge documents."""

    def __init__(
        self,
        *,
        kernel: DocumentKernelClient | None = None,
        stores: TaskDocumentStoreManager | None = None,
    ) -> None:
        self.kernel = kernel or DocumentKernelClient()
        self.stores = stores or TaskDocumentStoreManager()

    @staticmethod
    def document_id(task_id: str) -> str:
        return _stable_id("document", task_id)

    @staticmethod
    def document_path(task_id: str) -> str:
        # Hash the task id so caller-controlled text can never become a path.
        return f"tasks/{_stable_id('path', task_id)}.cowork"

    @staticmethod
    def binding_id(*, task_id: str, store_id: str, document_id: str) -> str:
        return hashlib.sha256(
            "\0".join(
                (
                    "tasks",
                    "task_knowledge",
                    task_id,
                    "task_knowledge",
                    store_id,
                    document_id,
                )
            ).encode("utf-8")
        ).hexdigest()[:32]

    def create(
        self,
        *,
        task_id: str,
        title: str,
        domain_revision: str,
        created_by: str,
        migration_origin: str | None = None,
        initial_markdown: str | bytes | None = None,
        document_generation: str | None = None,
        interaction_contract_id: str | None = None,
        initial_truth_activation: str | None = None,
        explicit_truth_acknowledged: bool = False,
        policy_intent_id: str | None = None,
        coordinator_decision_id: str | None = None,
        coordinator_decision_sha256: str | None = None,
        commit_admission: bool = True,
        task_admission_kind: str | None = None,
    ) -> TaskKnowledgeDocument:
        task_ref = str(task_id).strip()
        clean_title = str(title).strip()
        if not task_ref:
            raise ValueError("task_id is required")
        if not clean_title:
            raise ValueError("title is required")
        if task_admission_kind not in {
            None,
            "aggregate_creation/v2",
            "document_attachment/v1",
        }:
            raise ValueError("unsupported task admission kind")
        store = self.stores.ensure()
        identity_ref = (
            task_ref
            if document_generation is None
            else f"{task_ref}\0{document_generation}"
        )
        document_id = self.document_id(identity_ref)
        path = self.document_path(identity_ref)
        expected_binding_id = self.binding_id(
            task_id=task_ref,
            store_id=store.store_id,
            document_id=document_id,
        )
        from work_buddy.cowork.truth_activation import (
            WORKING_DOCUMENT_CONTRACT,
            provision_document_policy,
            resolve_document_truth_policy,
        )

        contract_id = interaction_contract_id or WORKING_DOCUMENT_CONTRACT
        activation = (
            "disabled"
            if interaction_contract_id is None and initial_truth_activation is None
            else initial_truth_activation
        )
        intent_id = policy_intent_id or _stable_id("policy-intent", identity_ref)
        created_new = False
        try:
            document = documents.get_document(store, document_id)
        except InvariantViolation:
            source = (
                initial_markdown.encode("utf-8")
                if isinstance(initial_markdown, str)
                else bytes(initial_markdown or b"")
            )
            newline_style = (
                "crlf"
                if b"\r\n" in source
                else "lf"
                if b"\n" in source
                else "cr"
                if b"\r" in source
                else "none"
            )
            trailing_newlines = 0
            cursor = len(source)
            while cursor > 0:
                if source[:cursor].endswith(b"\r\n"):
                    cursor -= 2
                elif source[:cursor].endswith((b"\n", b"\r")):
                    cursor -= 1
                else:
                    break
                trailing_newlines += 1
            outcome = self.kernel.request(
                {
                    "kind": "bootstrap_markdown",
                    "sourceBase64": source,
                    "sourceSha256": sha256_bytes(source),
                    "newlineStyle": newline_style,
                    "utf8Bom": source.startswith(b"\xef\xbb\xbf"),
                    "trailingNewlineCount": trailing_newlines,
                },
                request_id=f"task_bootstrap_{document_id}",
            )
            if outcome.snapshot is None or outcome.projection is None:
                raise RuntimeError("document_kernel_missing_result")
            snapshot_sha = ydoc_store.write_snapshot(
                store,
                snapshot=outcome.snapshot,
                expected_sha256=sha256_bytes(outcome.snapshot),
            )
            # The v11 portable recovery export requires every visible document
            # to carry its immutable interaction contract.  Register the
            # document and stage/commit its policy inside one Truth transaction
            # so the post-commit export can never observe the invalid gap.
            with store.write_transaction() as conn:
                document, _version, _created = documents.register_ready_document(
                    store,
                    path=path,
                    title=clean_title,
                    document_class="co_authored",
                    projection_bytes=outcome.projection,
                    ydoc_snapshot_sha256=snapshot_sha,
                    structured_head_sha256=structured_head_sha256(outcome.snapshot),
                    actor=Actor(kind="system", ref=created_by),
                    mode="create",
                    document_meta={
                        "domain_content": True,
                        "domain_namespace": "tasks",
                        "domain_entity_id": task_ref,
                        **(
                            {}
                            if task_admission_kind is None
                            else {
                                "task_admission": {
                                    "schema": "wb.task-document-admission/v1",
                                    "kind": task_admission_kind,
                                    "task_id": task_ref,
                                }
                            }
                        ),
                        "source": {
                            "kind": "domain_binding",
                            "writeback_policy": "never",
                        },
                    },
                    document_id=document_id,
                    version_id=_stable_id("version", identity_ref),
                    conn=conn,
                )
                provision_document_policy(
                    store,
                    document_id=document.id,
                    interaction_contract_id=contract_id,
                    binding_id=expected_binding_id,
                    initial_activation=activation,
                    explicit_truth_acknowledged=explicit_truth_acknowledged,
                    actor=created_by,
                    intent_id=intent_id,
                    coordinator_decision_id=coordinator_decision_id,
                    coordinator_decision_sha256=coordinator_decision_sha256,
                    commit_admission=commit_admission,
                    conn=conn,
                )
            created_new = True
        if not created_new:
            if task_admission_kind is not None:
                try:
                    meta = json.loads(document.meta_json or "{}")
                except json.JSONDecodeError as exc:
                    raise RuntimeError("task_document_admission_metadata_invalid") from exc
                if not isinstance(meta, dict) or meta.get("task_admission") != {
                    "schema": "wb.task-document-admission/v1",
                    "kind": task_admission_kind,
                    "task_id": task_ref,
                }:
                    raise RuntimeError("task_document_admission_metadata_mismatch")
            policy = resolve_document_truth_policy(store, document.id)
            if not policy.interaction_contract_id:
                provision_document_policy(
                    store,
                    document_id=document.id,
                    interaction_contract_id=contract_id,
                    binding_id=expected_binding_id,
                    initial_activation=activation,
                    explicit_truth_acknowledged=explicit_truth_acknowledged,
                    actor=created_by,
                    intent_id=intent_id,
                    coordinator_decision_id=coordinator_decision_id,
                    coordinator_decision_sha256=coordinator_decision_sha256,
                    commit_admission=commit_admission,
                )
        causality = DocumentCausalityStore(store.paths.sidecar)
        binding = causality.ensure_binding(
            domain_namespace="tasks",
            domain_kind="task_knowledge",
            domain_entity_id=task_ref,
            domain_revision=str(domain_revision),
            store_id=store.store_id,
            document_id=document.id,
            role="task_knowledge",
            created_by=created_by,
            projection_path=None,
            projection_mode="none",
            migration_origin=migration_origin,
        )
        if binding.content_authority == "domain":
            binding = causality.cutover_to_cowork(
                binding.binding_id,
                domain_revision=str(domain_revision),
            )
        if binding.binding_id != expected_binding_id:
            raise RuntimeError("task_document_binding_identity_mismatch")
        self.stores.registry.touch(store)
        return self._record(task_ref, store, document, binding)

    def reactivate_retired(
        self,
        *,
        task_id: str,
        store_id: str,
        document_id: str,
        binding_id: str,
        title: str,
        domain_revision: str,
        created_by: str,
    ) -> TaskKnowledgeDocument:
        """Create a born-Co-work successor while retaining retired history."""

        store = self.stores.open_existing()
        if store.store_id != store_id:
            raise RuntimeError("task_document_store_identity_mismatch")
        causality = DocumentCausalityStore(store.paths.sidecar)
        binding = causality.get_binding(binding_id)
        if binding is None:
            raise KeyError("task_document_binding_missing")
        if binding.lifecycle == "current":
            document = documents.get_document(store, binding.document_id)
            if documents.current_lifecycle(store, document.id) == "retired":
                raise RuntimeError("current_task_binding_targets_retired_document")
            return self._record(task_id, store, document, binding)
        if binding.lifecycle != "retired":
            raise RuntimeError("task_document_binding_not_restorable")
        retired = documents.get_document(store, document_id)
        if documents.current_lifecycle(store, retired.id) != "retired":
            raise RuntimeError("retired_task_binding_document_mismatch")
        source = store.resolve_blob_path(f"blobs/{retired.content_sha256}").read_bytes()
        return self.create(
            task_id=task_id,
            title=title,
            domain_revision=domain_revision,
            created_by=created_by,
            migration_origin=None,
            initial_markdown=source,
            document_generation=f"restore:{binding.binding_id}:{domain_revision}",
        )

    def get(self, task_id: str) -> TaskKnowledgeDocument | None:
        try:
            store = self.stores.open_existing()
        except FileNotFoundError:
            return None
        causality = DocumentCausalityStore(store.paths.sidecar)
        binding = causality.binding_for_domain(
            "tasks",
            "task_knowledge",
            task_id,
            "task_knowledge",
        )
        if binding is None:
            return None
        document = documents.get_document(store, binding.document_id)
        return self._record(task_id, store, document, binding)

    def read_markdown(self, task_id: str) -> str:
        """Read the live projection-free task document at its structured head."""

        store = self.stores.open_existing()
        causality = DocumentCausalityStore(store.paths.sidecar)
        binding = causality.binding_for_domain(
            "tasks",
            "task_knowledge",
            task_id,
            "task_knowledge",
        )
        if binding is None or binding.lifecycle != "current":
            raise KeyError(f"task knowledge document is unavailable: {task_id}")
        return project_live_markdown(store, binding.document_id, kernel=self.kernel)

    def append_markdown(
        self,
        *,
        task_id: str,
        markdown: str,
        actor_ref: str,
        idempotency_key: str,
        max_attempts: int = 3,
    ) -> TaskKnowledgeDocument:
        """CAS-append prose to the live Co-work head without a file projection.

        The current structured head is projected in memory, the requested
        section is appended, and a replacement snapshot is committed only if
        the head is unchanged.  Exact-content detection makes a response-loss
        retry idempotent; a concurrent editor causes a fresh projection/retry,
        never a last-write-wins overwrite.
        """

        section = str(markdown).strip()
        key = str(idempotency_key).strip()
        if not section:
            raise ValueError("markdown is required")
        if not key:
            raise ValueError("idempotency_key is required")
        store = self.stores.open_existing()
        causality = DocumentCausalityStore(store.paths.sidecar)
        binding = causality.binding_for_domain(
            "tasks",
            "task_knowledge",
            task_id,
            "task_knowledge",
        )
        if binding is None or binding.lifecycle != "current":
            raise KeyError(f"task knowledge document is unavailable: {task_id}")

        for attempt in range(max(1, max_attempts)):
            document = documents.get_document(store, binding.document_id)
            if document.ydoc_snapshot_sha256 is None:
                raise RuntimeError("task_document_snapshot_unavailable")
            snapshot = ydoc_store.read_snapshot(
                store,
                snapshot_sha256=document.ydoc_snapshot_sha256,
            )
            updates, _cursor = ydoc_store.read_updates(
                store,
                document_id=document.id,
            )
            base_head = structured_head_sha256(snapshot, updates)
            projected = self.kernel.request(
                {
                    "kind": "project_markdown",
                    "snapshotBase64": snapshot,
                    "updatesBase64": updates,
                    "expectedBaseStructuredHeadSha256": base_head,
                },
                request_id=(
                    f"task_append_project_{hashlib.sha256(key.encode()).hexdigest()[:20]}"
                    f"_{attempt}"
                ),
            )
            current_bytes = projected.projection
            if current_bytes is None:
                raise RuntimeError("document_kernel_missing_projection")
            current = current_bytes.decode("utf-8")
            if section in current:
                current_projection_sha = sha256_bytes(current_bytes)
                if document.content_sha256 != current_projection_sha:
                    store._store_blob_bytes(current_projection_sha, current_bytes)
                    document, _version, _event = documents.commit_document_version(
                        store,
                        document_id=document.id,
                        kind="materialized",
                        projection_sha256=current_projection_sha,
                        ydoc_snapshot_sha256=document.ydoc_snapshot_sha256,
                        structured_head_sha256=base_head,
                        actor=Actor(kind="system", ref=actor_ref),
                        detail="projection_free_task_append_recovery",
                    )
                return self._record(task_id, store, document, binding)
            combined = f"{current.rstrip()}\n\n{section}\n"
            replacement = self.kernel.request(
                {
                    "kind": "bootstrap_markdown",
                    "sourceBase64": combined.encode("utf-8"),
                    "sourceSha256": sha256_bytes(combined.encode("utf-8")),
                    "newlineStyle": "lf",
                    "utf8Bom": False,
                    "trailingNewlineCount": 1,
                },
                request_id=(
                    f"task_append_bootstrap_{hashlib.sha256(key.encode()).hexdigest()[:20]}"
                    f"_{attempt}"
                ),
            )
            if replacement.snapshot is None or replacement.projection is None:
                raise RuntimeError("document_kernel_missing_result")
            projection_sha = sha256_bytes(replacement.projection)
            store._store_blob_bytes(projection_sha, replacement.projection)
            try:
                new_snapshot_sha, new_head, _cursor, _receipt = ydoc_store.compact_and_advance(
                    store,
                    document_id=document.id,
                    snapshot=replacement.snapshot,
                    expected_snapshot_sha256=sha256_bytes(replacement.snapshot),
                    expected_structured_head_sha256=base_head,
                    actor=Actor(kind="system", ref=actor_ref),
                    projection_sha256=projection_sha,
                )
            except ydoc_store.StructuredHeadConflict:
                if attempt + 1 >= max(1, max_attempts):
                    raise
                continue
            refreshed, _version, _event = documents.commit_document_version(
                store,
                document_id=document.id,
                kind="materialized",
                projection_sha256=projection_sha,
                ydoc_snapshot_sha256=new_snapshot_sha,
                structured_head_sha256=new_head,
                actor=Actor(kind="system", ref=actor_ref),
                detail="projection_free_task_append",
            )
            self.stores.registry.touch(store)
            return self._record(task_id, store, refreshed, binding)
        raise RuntimeError("task_document_append_conflict")

    @staticmethod
    def _record(
        task_id: str,
        store: TruthStore,
        document,
        binding: DomainDocumentBinding,
    ) -> TaskKnowledgeDocument:
        return TaskKnowledgeDocument(
            task_id=task_id,
            store_id=store.store_id,
            document_id=document.id,
            binding_id=binding.binding_id,
            title=document.title or "Task knowledge",
            lifecycle=documents.current_lifecycle(store, document.id),
        )


__all__ = [
    "TaskDocumentService",
    "TaskDocumentStoreManager",
    "TaskKnowledgeDocument",
    "project_live_markdown",
]
