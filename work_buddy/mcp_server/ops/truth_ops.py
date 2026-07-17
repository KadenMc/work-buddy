"""Agent-facing operations for scoped Truth stores.

Every addressed operation resolves the store through the machine registry.
Agent writes derive durable producer identity from the gateway-injected session
and its manifest. Human decisions are separately authorized per invocation,
rendered from durable rows, and committed with one exact gesture.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from work_buddy.consent import (
    ConsentPrompt,
    current_per_invocation_authorization,
    requires_consent,
)
from work_buddy.mcp_server.op_registry import register_op
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor, InvariantViolation, StorePaths
from work_buddy.truth.events import emit_truth_event
from work_buddy.truth.identity import (
    canonical_claim_payload,
    claim_sha256,
    new_id,
    parse_truth_uri,
    sha256_text,
)
from work_buddy.truth.lifecycle import (
    CONFIRM_GESTURE_KINDS,
    REJECTION_CLASSES,
    TruthLifecycle,
    negated_proposition,
)
from work_buddy.truth.locators import validate_locator
from work_buddy.truth.profiles import normalize_store_id, validate_new_claim
from work_buddy.truth.queries import (
    PremiseResolution,
    SweepFindingSpec,
    claims_as_of,
    conflicts,
    current_claims,
    integrity_findings,
    needs_review,
    record_sweep,
    source_sweep_candidates,
    supersession_sweep_candidates,
)
from work_buddy.truth.redact import REDACTION_REASONS, TruthRedactor
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.review import ClaimReviewPayload, compose_claim_review
from work_buddy.truth.store import TruthStore


_HUMAN = Actor("human", "work-buddy-user")
_CONSENT_SURFACE = "chat_consent"
_TERMINAL_STATUSES = frozenset({"rejected", "expired", "superseded", "retracted"})
_AGENT_ONLY_RESULT_WARNING = (
    "WARNING: The rejection result is supported only by agent-authored evidence. "
    "Approving this rejection also confirms that result against agent-authored support."
)
_IDENTITY_PLACEHOLDERS = frozenset(
    {"unknown", "none", "null", "unspecified", "n/a", "na"}
)


def _registry() -> TruthStoreRegistry:
    return TruthStoreRegistry()


def _reserve_new_sidecar(paths: StorePaths) -> Path:
    """Reserve an absent sidecar and return its unforgeable cleanup marker.

    An existing path is never claimed by this invocation.  The marker lets a
    failed create prove it still owns the directory before recursive cleanup.
    """

    try:
        paths.sidecar.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise InvariantViolation(
            f"truth sidecar already exists: {paths.sidecar}"
        ) from exc
    marker = paths.sidecar / f".store-create-{new_id()}.pending"
    try:
        marker.touch(exist_ok=False)
    except Exception as exc:
        try:
            paths.sidecar.rmdir()
        except OSError as cleanup_exc:
            raise InvariantViolation(
                "truth sidecar reservation failed and cleanup was incomplete: "
                f"{cleanup_exc}"
            ) from exc
        raise
    return marker


def _rollback_store_create(
    registry: TruthStoreRegistry,
    paths: StorePaths,
    *,
    marker: Path,
) -> tuple[str, ...]:
    """Compensate only state this invocation can prove it introduced."""

    failures: list[str] = []
    try:
        registry.unregister(paths.sidecar)
    except Exception as exc:
        failures.append(f"registry cleanup failed: {exc}")
    try:
        if not marker.is_file():
            raise InvariantViolation(
                "sidecar ownership marker is missing; refusing recursive cleanup"
            )
        shutil.rmtree(paths.sidecar)
    except Exception as exc:
        failures.append(f"sidecar cleanup failed: {exc}")
    return tuple(failures)


def _serialize(value: Any) -> Any:
    """Convert Truth models to gateway-safe JSON values."""

    if is_dataclass(value) and not isinstance(value, type):
        return _serialize(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_serialize(item) for item in value]
    return value


def _event_result(emission: Any) -> Any:
    return _serialize(emission)


def _deterministic_record_id(seed: str) -> str:
    """Derive a stable UUID4-shaped record id for a reviewed future row."""

    characters = list(sha256_text(seed)[:32])
    characters[12] = "4"
    characters[16] = "a"
    return "".join(characters)


def _open_store(store_id: str) -> TruthStore:
    return _registry().open_store(store_id)


def _session_manifest(session_id: str) -> Mapping[str, Any]:
    from work_buddy.agent_session import list_sessions

    for manifest in list_sessions():
        if str(manifest.get("session_id") or "") == session_id:
            return manifest
        if str(manifest.get("native_session_id") or "") == session_id:
            return manifest
    return {}


def _identity_value(value: Any) -> str:
    """Normalize one producer identity value and discard placeholders."""

    normalized = str(value or "").strip()
    return "" if normalized.casefold() in _IDENTITY_PLACEHOLDERS else normalized


def _agent_actor(
    *,
    producer_model: str,
    agent_session_id: str | None,
    producer_call_id: str | None,
) -> Actor:
    requested_session_id = _identity_value(agent_session_id)
    claimed_model = _identity_value(producer_model)
    if not requested_session_id:
        raise InvariantViolation("agent write is missing the gateway session identity")
    manifest = _session_manifest(requested_session_id)
    session_id = _identity_value(manifest.get("session_id"))
    harness = _identity_value(manifest.get("harness_id"))
    manifest_model = _identity_value(manifest.get("model"))
    missing = [
        name
        for name, value in (
            ("session_id", session_id),
            ("harness", harness),
            ("model", claimed_model),
        )
        if not value
    ]
    if missing:
        raise InvariantViolation(
            "agent write is missing producer identity fields: " + ", ".join(missing)
        )
    if manifest_model and claimed_model != manifest_model:
        raise InvariantViolation(
            "producer_model does not match the session manifest model"
        )
    model = manifest_model or claimed_model
    model_source = "session_manifest" if manifest_model else "caller_asserted"
    producer: dict[str, Any] = {
        "model": model,
        "model_source": model_source,
        "harness": harness,
        "surface": "mcp",
        "session_id": session_id,
    }
    call_id = str(producer_call_id or "").strip()
    if call_id:
        producer["call_id"] = call_id
    return Actor("agent_run", session_id, producer)


def _with_model_source(
    actor: Actor,
    meta: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Add the model verification source to durable record metadata."""

    durable = dict(meta or {})
    model_source = str(actor.meta["model_source"])
    if "model_source" in durable and durable["model_source"] != model_source:
        raise InvariantViolation(
            "caller meta conflicts with authoritative actor field 'model_source'"
        )
    durable["model_source"] = model_source
    return durable


def _require_consent_surface(store: TruthStore) -> None:
    if _CONSENT_SURFACE not in store.profile.gate.confirmation_surfaces:
        raise InvariantViolation(
            "truth store profile does not allow the chat_consent decision surface"
        )


def _authorization() -> Any:
    authorization = current_per_invocation_authorization()
    if authorization is None:
        raise InvariantViolation("per-invocation Truth authorization is missing")
    return authorization


def _review_context(
    store: TruthStore,
    review: ClaimReviewPayload,
) -> dict[str, Any]:
    return {
        "store_id": store.store_id,
        "store_path": str(store.paths.sidecar),
        "profile": store.profile.profile,
        "action": review.action,
        "claim_id": review.claim_id,
        "claim_payload": _serialize(review.claim_payload),
        "payload_sha256": review.payload_sha256,
        "context_sha256": review.context_sha256,
        "receipts": [_serialize(item) for item in review.receipts],
        "agent_authored_only": review.agent_authored_only,
        "decision": _serialize(review.decision),
    }


def _prompt_for_review(store: TruthStore, review: ClaimReviewPayload) -> ConsentPrompt:
    _require_consent_surface(store)
    return ConsentPrompt(
        body=review.body,
        fingerprint=review.request_fingerprint,
        context=_review_context(store, review),
    )


def _assert_authorized_review(review: ClaimReviewPayload) -> Any:
    authorization = _authorization()
    if authorization.fingerprint != review.request_fingerprint:
        raise InvariantViolation("authorized Truth review no longer matches durable state")
    return authorization


def _existing_support_link(
    conn: Any,
    claim_id: str,
    span_id: str,
) -> str | None:
    row = conn.execute(
        "SELECT l.id FROM claim_links AS l "
        "LEFT JOIN link_retractions AS r ON r.link_id = l.id "
        "WHERE l.from_claim_id = ? AND l.link_type = 'supports_span' "
        "AND l.to_kind = 'evidence_span' AND l.to_ref = ? "
        "AND r.link_id IS NULL ORDER BY l.created_at, l.id LIMIT 1",
        (claim_id, span_id),
    ).fetchone()
    return None if row is None else str(row["id"])


def _attach_supports(
    store: TruthStore,
    *,
    claim_id: str,
    span_ids: Sequence[str] | None,
    actor: Actor,
    conn: Any,
) -> tuple[list[Any], int]:
    links: list[Any] = []
    created = 0
    seen: set[str] = set()
    for raw_span_id in span_ids or ():
        span_id = str(raw_span_id).strip().lower()
        if not span_id or span_id in seen:
            continue
        seen.add(span_id)
        existing_id = _existing_support_link(conn, claim_id, span_id)
        if existing_id is not None:
            existing = store.get_link(existing_id, conn=conn)
            if existing is not None:
                links.append(existing)
            continue
        links.append(
            store.add_link(
                from_claim_id=claim_id,
                link_type="supports_span",
                to_kind="evidence_span",
                to_ref=span_id,
                actor=actor,
                conn=conn,
            )
        )
        created += 1
    return links, created


def _attach_derivation(
    store: TruthStore,
    *,
    claim_id: str,
    derivation: Mapping[str, Any] | None,
    actor: Actor,
    conn: Any,
) -> Any | None:
    if derivation is None:
        return None
    if not isinstance(derivation, Mapping):
        raise InvariantViolation("derivation must be a mapping")
    method = derivation.get("method")
    premises = derivation.get("premises")
    if not isinstance(method, str) or not method.strip():
        raise InvariantViolation("derivation requires a nonempty method")
    if not isinstance(premises, Sequence) or isinstance(premises, (str, bytes)):
        raise InvariantViolation("derivation premises must be a list")
    return store.add_derivation(
        claim_id=claim_id,
        method=method,
        premises=premises,
        actor=actor,
        confidence=derivation.get("confidence"),
        rationale=derivation.get("rationale"),
        conn=conn,
    )


def _support_receipts(
    store: TruthStore,
    span_ids: Sequence[str] | None,
) -> list[dict[str, Any]]:
    identifiers = tuple(dict.fromkeys(str(item).strip().lower() for item in span_ids or ()))
    if not identifiers:
        return []
    receipts: list[dict[str, Any]] = []
    with store.connect() as conn:
        for span_id in identifiers:
            row = conn.execute(
                "SELECT s.id AS span_id, s.span_sha256, s.quote_exact, "
                "e.id AS evidence_id, e.kind AS evidence_kind, "
                "e.source_locator, e.content_sha256, e.trust_class, "
                "s.author_kind, s.author_ref, e.derived_from_store "
                "FROM evidence_spans AS s JOIN evidence AS e "
                "ON e.id = s.evidence_id WHERE s.id = ? "
                "AND s.redacted_at IS NULL AND e.redacted_at IS NULL",
                (span_id,),
            ).fetchone()
            if row is None:
                raise InvariantViolation(
                    f"support span does not exist or is redacted: {span_id}"
                )
            receipts.append(dict(row))
    return receipts


@requires_consent(
    "truth.store_create",
    "Create and register a new scoped Truth store on disk.",
    risk="moderate",
)
def truth_store_create(
    root: str,
    profile: Mapping[str, Any],
    store_id: str | None = None,
) -> dict[str, Any]:
    """Create a scoped store, then register its validated identity."""

    if not isinstance(profile, Mapping):
        raise InvariantViolation("profile must be a mapping")
    values = dict(profile)
    profile_store_id = values.get("store_id")
    if store_id is not None and profile_store_id is not None:
        if normalize_store_id(store_id) != normalize_store_id(profile_store_id):
            raise InvariantViolation("store_id conflicts with profile.store_id")
    requested = normalize_store_id(store_id or profile_store_id or new_id())
    values["store_id"] = requested
    registry = _registry()
    if registry.get_by_store_id(requested) is not None:
        raise InvariantViolation(f"truth store identity is already registered: {requested}")
    paths = StorePaths.from_root(root)
    if registry.get_by_path(paths.sidecar, refresh=False) is not None:
        raise InvariantViolation(f"truth store path is already registered: {paths.sidecar}")
    marker = _reserve_new_sidecar(paths)
    try:
        store = TruthStore.create(root, values)
        registered = registry.register(store)
        marker.unlink()
    except Exception as exc:
        rollback_failures = _rollback_store_create(
            registry,
            paths,
            marker=marker,
        )
        if rollback_failures:
            detail = "; ".join(rollback_failures)
            raise InvariantViolation(
                f"truth store creation failed ({exc}); rollback incomplete: {detail}"
            ) from exc
        raise
    emission = emit_truth_event(
        "truth.store_created",
        store_id=store.store_id,
        data={"path": str(store.paths.sidecar), "profile": store.profile.profile},
    )
    return {
        "ok": True,
        "store": _serialize(registered),
        "event": _event_result(emission),
    }


def truth_store_list(refresh: bool = True) -> dict[str, Any]:
    """List registered Truth stores and their current reachability."""

    stores = _registry().list_stores(refresh=refresh)
    return {"ok": True, "count": len(stores), "stores": _serialize(stores)}


def truth_evidence_capture(
    store_id: str,
    kind: str,
    source_locator: str,
    acquisition_method: str,
    producer_model: str,
    content: str | None = None,
    content_sha256: str | None = None,
    media_type: str | None = None,
    acquired_at: str | None = None,
    origin: str | None = None,
    external_reviewed: bool = False,
    derived_from_store: str | None = None,
    meta: Mapping[str, Any] | None = None,
    producer_call_id: str | None = None,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    """Validate and capture one immutable evidence record."""

    actor = _agent_actor(
        producer_model=producer_model,
        agent_session_id=agent_session_id,
        producer_call_id=producer_call_id,
    )
    store = _open_store(store_id)
    digest = content_sha256
    if content is not None:
        computed = sha256_text(content)
        if digest is not None and str(digest).lower() != computed:
            raise InvariantViolation("supplied content_sha256 does not match content")
        digest = computed
    validation = validate_locator(kind, source_locator, meta, digest)
    durable_meta = _with_model_source(actor, validation.meta)
    durable_meta["verifiability_class"] = validation.verifiability_class
    durable_meta["integrity_recipe"] = dict(validation.integrity_recipe)
    evidence = store.capture_evidence(
        kind=validation.kind,
        source_locator=validation.locator,
        actor=actor,
        acquisition_method=acquisition_method,
        content=content,
        content_sha256=validation.content_sha256,
        media_type=media_type,
        acquired_at=acquired_at,
        origin=origin,
        external_reviewed=external_reviewed,
        derived_from_store=derived_from_store,
        meta=durable_meta,
    )
    emission = emit_truth_event(
        "truth.evidence_captured",
        store_id=store.store_id,
        subject_kind="evidence",
        subject_id=evidence.id,
        data={"kind": evidence.kind, "trust_class": evidence.trust_class},
    )
    return {
        "ok": True,
        "evidence": _serialize(evidence),
        "locator": _serialize(validation),
        "event": _event_result(emission),
    }


def truth_span_mark(
    store_id: str,
    evidence_id: str,
    selector: Mapping[str, Any],
    producer_model: str,
    snapshot_text: str | None = None,
    author_kind: str | None = None,
    author_ref: str | None = None,
    producer_call_id: str | None = None,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    """Resolve and append one evidence span."""

    if not isinstance(selector, Mapping):
        raise InvariantViolation("selector must be a mapping")
    actor = _agent_actor(
        producer_model=producer_model,
        agent_session_id=agent_session_id,
        producer_call_id=producer_call_id,
    )
    store = _open_store(store_id)
    span = store.mark_span(
        evidence_id=evidence_id,
        selector=CompositeSelector(
            exact=selector.get("exact"),
            prefix=selector.get("prefix", ""),
            suffix=selector.get("suffix", ""),
            start=selector.get("start"),
            end=selector.get("end"),
        ),
        actor=actor,
        author_kind=author_kind,
        author_ref=author_ref,
        snapshot_text=snapshot_text,
    )
    emission = emit_truth_event(
        "truth.span_marked",
        store_id=store.store_id,
        subject_kind="span",
        subject_id=span.id,
        data={"evidence_id": span.evidence_id},
    )
    return {"ok": True, "span": _serialize(span), "event": _event_result(emission)}


def truth_claim_propose(
    store_id: str,
    proposition: str,
    claim_kind: str,
    producer_model: str,
    structured: Mapping[str, Any] | None = None,
    scope: str = "store",
    valid_from: str | None = None,
    valid_to: str | None = None,
    confidence_extraction: float | None = None,
    meta: Mapping[str, Any] | None = None,
    support_span_ids: Sequence[str] | None = None,
    derivation: Mapping[str, Any] | None = None,
    producer_call_id: str | None = None,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    """Propose a claim with optional support and derivation in one commit."""

    actor = _agent_actor(
        producer_model=producer_model,
        agent_session_id=agent_session_id,
        producer_call_id=producer_call_id,
    )
    store = _open_store(store_id)
    with store.write_transaction() as conn:
        written = store.propose_claim(
            proposition=proposition,
            claim_kind=claim_kind,
            actor=actor,
            structured=structured,
            scope=scope,
            valid_from=valid_from,
            valid_to=valid_to,
            confidence_extraction=confidence_extraction,
            meta=_with_model_source(actor, meta),
            conn=conn,
        )
        links, _ = _attach_supports(
            store,
            claim_id=written.claim.id,
            span_ids=support_span_ids,
            actor=actor,
            conn=conn,
        )
        derived = _attach_derivation(
            store,
            claim_id=written.claim.id,
            derivation=derivation,
            actor=actor,
            conn=conn,
        )
    emission = (
        emit_truth_event(
            "truth.claim_proposed",
            store_id=store.store_id,
            subject_kind="claim",
            subject_id=written.claim.id,
            data={"created": True},
        )
        if written.created
        else None
    )
    return {
        "ok": True,
        "claim": _serialize(written.claim),
        "created": written.created,
        "support_links": _serialize(links),
        "derivation": _serialize(derived),
        "event": _event_result(emission),
    }


def _confirm_prompt(
    store_id: str,
    claim_id: str,
    gesture_kind: str = "confirm",
) -> ConsentPrompt:
    store = _open_store(store_id)
    if gesture_kind not in CONFIRM_GESTURE_KINDS | {"confirm_quarantined_support"}:
        raise InvariantViolation("unsupported confirmation gesture kind")
    review = compose_claim_review(
        store,
        claim_id,
        action="confirm",
        decision={"gesture_kind": gesture_kind},
    )
    return _prompt_for_review(store, review)


@requires_consent(
    "truth.claim_confirm",
    "Confirm this exact Truth claim and its displayed receipts.",
    risk="high",
    consent_weight="high",
    grant_policy="per_invocation",
    request_factory=_confirm_prompt,
)
def truth_claim_confirm(
    store_id: str,
    claim_id: str,
    gesture_kind: str = "confirm",
) -> dict[str, Any]:
    """Confirm one exact claim using the immediately approved review."""

    store = _open_store(store_id)
    _require_consent_surface(store)
    lifecycle = TruthLifecycle(store)
    with store.write_transaction() as conn:
        review = compose_claim_review(
            store,
            claim_id,
            action="confirm",
            decision={"gesture_kind": gesture_kind},
        )
        authorization = _assert_authorized_review(review)
        gesture = lifecycle.mint_gesture(
            subject_ref=claim_id,
            actor=_HUMAN,
            surface=_CONSENT_SURFACE,
            kind=gesture_kind,
            displayed_payload_sha256=review.payload_sha256,
            context_sha256=review.context_sha256,
            conn=conn,
        )
        result = lifecycle.confirm_claim(
            claim_id=claim_id,
            gesture_id=gesture.id,
            actor=_HUMAN,
            expected_context_sha256=review.context_sha256,
            conn=conn,
        )
    status = "needs_review" if result.needs_review_event is not None else "confirmed"
    emission = (
        emit_truth_event(
            "truth.claim_confirmed",
            store_id=store.store_id,
            subject_kind="claim",
            subject_id=claim_id,
            data={"status": status, "gesture_id": gesture.id},
        )
        if result.created
        else None
    )
    return {
        "ok": True,
        "result": _serialize(result),
        "authorization": {
            "request_id": authorization.request_id,
            "response_surface": authorization.response_surface,
        },
        "event": _event_result(emission),
    }


def _live_claim_id_for_hash(store: TruthStore, canonical_sha256: str) -> str | None:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT c.id, (SELECT e.status FROM claim_status_events AS e "
            "WHERE e.claim_id = c.id AND e.status != 'needs_review' "
            "ORDER BY e.seq DESC LIMIT 1) AS status "
            "FROM claims AS c WHERE c.canonical_sha256 = ? "
            "AND c.redacted_at IS NULL ORDER BY c.created_at, c.id",
            (canonical_sha256,),
        ).fetchall()
    live = [row["id"] for row in rows if row["status"] not in _TERMINAL_STATUSES]
    if len(live) > 1:
        raise InvariantViolation("canonical claim hash resolves to multiple live claims")
    return None if not live else str(live[0])


def _rejection_spec(
    store: TruthStore,
    claim_id: str,
    *,
    reason_class: str,
    result_proposition: str | None,
    result_structured: Mapping[str, Any] | None,
    support_span_ids: Sequence[str] | None,
    derivation: Mapping[str, Any] | None,
) -> tuple[ClaimReviewPayload, Any, dict[str, Any] | None]:
    if reason_class not in REJECTION_CLASSES:
        raise InvariantViolation(f"unsupported rejection class: {reason_class}")
    if reason_class == "reject_plain" and (
        result_proposition is not None
        or result_structured is not None
        or bool(support_span_ids)
        or derivation is not None
    ):
        raise InvariantViolation("reject_plain cannot carry a result claim payload")
    if reason_class == "reject_as_false" and (
        result_proposition is not None or result_structured is not None
    ):
        raise InvariantViolation(
            "reject_as_false derives its result from the source claim"
        )
    source = store.get_claim(claim_id)
    if source is None:
        raise InvariantViolation(f"claim does not exist: {claim_id}")
    initial = compose_claim_review(store, claim_id, action="reject")
    source_receipts = [_serialize(item) for item in initial.receipts]
    result_spec: dict[str, Any] | None = None
    if reason_class != "reject_plain":
        if reason_class == "reject_as_false":
            proposition = negated_proposition(source.proposition)
            claim_kind = source.claim_kind
            structured = (
                None
                if source.structured_json is None
                else json.loads(source.structured_json)
            )
        else:
            proposition = str(result_proposition or "").strip()
            if not proposition:
                raise InvariantViolation(
                    "reject_as_preference requires result_proposition"
                )
            claim_kind = "preference"
            structured = result_structured
        validate_new_claim(
            store.profile,
            claim_kind=claim_kind,
            structured=structured,
        )
        payload = canonical_claim_payload(
            proposition=proposition,
            claim_kind=claim_kind,
            structured=structured,
            scope=source.scope,
            valid_from=source.valid_from,
            valid_to=source.valid_to,
        )
        canonical = claim_sha256(
            proposition=proposition,
            claim_kind=claim_kind,
            structured=structured,
            scope=source.scope,
            valid_from=source.valid_from,
            valid_to=source.valid_to,
        )
        result_id = _live_claim_id_for_hash(store, canonical) or _deterministic_record_id(
            "|".join(
                (
                    "truth-rejection-result",
                    store.store_id,
                    source.id,
                    reason_class,
                    canonical,
                )
            )
        )[:32]
        support_receipts = _support_receipts(store, support_span_ids)
        usable_support = [
            item for item in support_receipts if item["derived_from_store"] is None
        ]
        agent_authored_only = bool(usable_support) and all(
            item["trust_class"] == "agent_authored"
            or item["author_kind"] == "agent_run"
            for item in usable_support
        )
        result_spec = {
            "id": result_id,
            "proposition": proposition,
            "claim_kind": claim_kind,
            "structured": structured,
            "scope": source.scope,
            "valid_from": source.valid_from,
            "valid_to": source.valid_to,
            "canonical_payload": payload,
            "canonical_sha256": canonical,
            "support_receipts": support_receipts,
            "agent_authored_only": agent_authored_only,
            "support_warning": (
                _AGENT_ONLY_RESULT_WARNING if agent_authored_only else None
            ),
            "derivation": _serialize(derivation),
        }
    receipts: Any = source_receipts
    if result_spec is not None:
        receipts = {
            "source": source_receipts,
            "result_support": result_spec["support_receipts"],
        }
    bound_context = TruthLifecycle(store).rejection_context_sha256(claim_id, receipts)
    decision = {
        "reason_class": reason_class,
        "rejection_context_sha256": bound_context,
        "result": result_spec,
    }
    review = compose_claim_review(
        store,
        claim_id,
        action="reject",
        decision=decision,
    )
    return review, receipts, result_spec


def _reject_prompt(
    store_id: str,
    claim_id: str,
    reason_class: str,
    result_proposition: str | None = None,
    result_structured: Mapping[str, Any] | None = None,
    support_span_ids: Sequence[str] | None = None,
    derivation: Mapping[str, Any] | None = None,
) -> ConsentPrompt:
    store = _open_store(store_id)
    review, _, _ = _rejection_spec(
        store,
        claim_id,
        reason_class=reason_class,
        result_proposition=result_proposition,
        result_structured=result_structured,
        support_span_ids=support_span_ids,
        derivation=derivation,
    )
    return _prompt_for_review(store, review)


@requires_consent(
    "truth.claim_reject",
    "Reject this exact Truth claim using the displayed reason class.",
    risk="high",
    consent_weight="high",
    grant_policy="per_invocation",
    request_factory=_reject_prompt,
)
def truth_claim_reject(
    store_id: str,
    claim_id: str,
    reason_class: str,
    result_proposition: str | None = None,
    result_structured: Mapping[str, Any] | None = None,
    support_span_ids: Sequence[str] | None = None,
    derivation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply one exact reason-classed rejection after human approval."""

    store = _open_store(store_id)
    _require_consent_surface(store)
    lifecycle = TruthLifecycle(store)
    with store.write_transaction() as conn:
        review, receipts, result_spec = _rejection_spec(
            store,
            claim_id,
            reason_class=reason_class,
            result_proposition=result_proposition,
            result_structured=result_structured,
            support_span_ids=support_span_ids,
            derivation=derivation,
        )
        authorization = _assert_authorized_review(review)
        context_sha256 = lifecycle.rejection_context_sha256(
            claim_id,
            receipts,
            conn=conn,
        )
        result_claim = None
        support_links: list[Any] = []
        derivation_record = None
        gesture_subject = store.get_claim(claim_id, conn=conn)
        if gesture_subject is None:
            raise InvariantViolation(f"claim does not exist: {claim_id}")
        if result_spec is not None:
            written = store.propose_claim(
                proposition=result_spec["proposition"],
                claim_kind=result_spec["claim_kind"],
                actor=_HUMAN,
                structured=result_spec["structured"],
                scope=result_spec["scope"],
                valid_from=result_spec["valid_from"],
                valid_to=result_spec["valid_to"],
                meta={"rejection_source_claim_id": claim_id},
                record_id=result_spec["id"],
                conn=conn,
            )
            result_claim = written.claim
            if result_claim.canonical_sha256 != result_spec["canonical_sha256"]:
                raise InvariantViolation("rejection result changed after approval")
            support_links, _ = _attach_supports(
                store,
                claim_id=result_claim.id,
                span_ids=support_span_ids,
                actor=_HUMAN,
                conn=conn,
            )
            derivation_record = _attach_derivation(
                store,
                claim_id=result_claim.id,
                derivation=derivation,
                actor=_HUMAN,
                conn=conn,
            )
            gesture_subject = result_claim
        gesture = lifecycle.mint_gesture(
            subject_ref=gesture_subject.id,
            actor=_HUMAN,
            surface=_CONSENT_SURFACE,
            kind=reason_class,
            displayed_payload_sha256=gesture_subject.canonical_sha256,
            context_sha256=context_sha256,
            conn=conn,
        )
        result = lifecycle.reject_claim(
            source_claim_id=claim_id,
            result_claim_id=(None if result_claim is None else result_claim.id),
            gesture_id=gesture.id,
            actor=_HUMAN,
            reason_class=reason_class,
            expected_context_sha256=context_sha256,
            displayed_receipts=receipts,
            conn=conn,
        )
    emission = emit_truth_event(
        "truth.claim_rejected",
        store_id=store.store_id,
        subject_kind="claim",
        subject_id=claim_id,
        data={
            "reason_class": reason_class,
            "gesture_id": gesture.id,
            "result_claim_id": None if result_claim is None else result_claim.id,
        },
    )
    return {
        "ok": True,
        "result": _serialize(result),
        "support_links": _serialize(support_links),
        "derivation": _serialize(derivation_record),
        "authorization": {
            "request_id": authorization.request_id,
            "response_surface": authorization.response_surface,
        },
        "event": _event_result(emission),
    }


def truth_claim_challenge(
    store_id: str,
    claim_id: str,
    challenging_claim_id: str,
    producer_model: str,
    note: str | None = None,
    producer_call_id: str | None = None,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    """Challenge a confirmed claim using one supported live claim."""

    actor = _agent_actor(
        producer_model=producer_model,
        agent_session_id=agent_session_id,
        producer_call_id=producer_call_id,
    )
    store = _open_store(store_id)
    result = TruthLifecycle(store).challenge_claim(
        claim_id=claim_id,
        challenging_claim_id=challenging_claim_id,
        actor=actor,
        note=note,
    )
    emission = (
        emit_truth_event(
            "truth.claim_challenged",
            store_id=store.store_id,
            subject_kind="claim",
            subject_id=claim_id,
            data={"challenging_claim_id": challenging_claim_id},
        )
        if result.created
        else None
    )
    return {"ok": True, "result": _serialize(result), "event": _event_result(emission)}


def truth_claim_supersede(
    store_id: str,
    predecessor_claim_id: str,
    reason: str,
    producer_model: str,
    successor_claim_id: str | None = None,
    proposition: str | None = None,
    claim_kind: str | None = None,
    structured: Mapping[str, Any] | None = None,
    scope: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    confidence_extraction: float | None = None,
    meta: Mapping[str, Any] | None = None,
    support_span_ids: Sequence[str] | None = None,
    derivation: Mapping[str, Any] | None = None,
    note: str | None = None,
    producer_call_id: str | None = None,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    """Create or select a successor, then append its supersession link."""

    if successor_claim_id is not None:
        new_successor_args = {
            "proposition": proposition,
            "claim_kind": claim_kind,
            "structured": structured,
            "scope": scope,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "confidence_extraction": confidence_extraction,
            "meta": meta,
        }
        invalid = [
            name for name, value in new_successor_args.items() if value is not None
        ]
        if invalid:
            raise InvariantViolation(
                "existing successor_claim_id cannot be combined with "
                f"new-successor parameters: {', '.join(invalid)}"
            )
    actor = _agent_actor(
        producer_model=producer_model,
        agent_session_id=agent_session_id,
        producer_call_id=producer_call_id,
    )
    store = _open_store(store_id)
    with store.write_transaction() as conn:
        created = False
        if successor_claim_id is None:
            if not str(proposition or "").strip() or not str(claim_kind or "").strip():
                raise InvariantViolation(
                    "supersession requires successor_claim_id or proposition and claim_kind"
                )
            written = store.propose_claim(
                proposition=str(proposition),
                claim_kind=str(claim_kind),
                actor=actor,
                structured=structured,
                scope="store" if scope is None else scope,
                valid_from=valid_from,
                valid_to=valid_to,
                confidence_extraction=confidence_extraction,
                meta=_with_model_source(actor, meta),
                conn=conn,
            )
            successor = written.claim
            created = written.created
        else:
            successor = store.get_claim(successor_claim_id, conn=conn)
            if successor is None:
                raise InvariantViolation(
                    f"successor claim does not exist: {successor_claim_id}"
                )
        links, _ = _attach_supports(
            store,
            claim_id=successor.id,
            span_ids=support_span_ids,
            actor=actor,
            conn=conn,
        )
        derived = _attach_derivation(
            store,
            claim_id=successor.id,
            derivation=derivation,
            actor=actor,
            conn=conn,
        )
        existing_supersession = conn.execute(
            "SELECT l.id, l.role_json FROM claim_links AS l "
            "LEFT JOIN link_retractions AS r ON r.link_id = l.id "
            "WHERE l.from_claim_id = ? AND l.link_type = 'supersedes' "
            "AND l.to_kind = 'claim' AND l.to_ref = ? AND r.link_id IS NULL "
            "ORDER BY l.id LIMIT 1",
            (successor.id, predecessor_claim_id),
        ).fetchone()
        supersedes = TruthLifecycle(store).supersede_claim(
            successor_claim_id=successor.id,
            predecessor_claim_id=predecessor_claim_id,
            reason=reason,
            actor=actor,
            note=note,
            conn=conn,
        )
        link_created = existing_supersession is None
    emission = (
        emit_truth_event(
            "truth.claim_superseded",
            store_id=store.store_id,
            subject_kind="claim",
            subject_id=successor.id,
            data={
                "predecessor_claim_id": predecessor_claim_id,
                "reason": reason,
                "link_id": supersedes.id,
            },
        )
        if link_created
        else None
    )
    return {
        "ok": True,
        "successor": _serialize(successor),
        "created": created,
        "link_created": link_created,
        "supersedes_link": _serialize(supersedes),
        "support_links": _serialize(links),
        "derivation": _serialize(derived),
        "event": _event_result(emission),
    }


def _redact_prompt(store_id: str, claim_id: str, reason: str = "privacy") -> ConsentPrompt:
    if reason not in REDACTION_REASONS:
        raise InvariantViolation(f"unsupported redaction reason: {reason}")
    store = _open_store(store_id)
    claim = store.get_claim(claim_id)
    if claim is None:
        raise InvariantViolation(f"claim does not exist: {claim_id}")
    if claim.redacted_at is not None:
        raise InvariantViolation("claim content is already redacted")
    review = compose_claim_review(
        store,
        claim_id,
        action="redact",
        decision={"reason": reason, "subject_kind": "claim"},
    )
    return _prompt_for_review(store, review)


@requires_consent(
    "truth.claim_redact",
    "Destroy the readable content of this exact Truth claim.",
    risk="high",
    consent_weight="high",
    grant_policy="per_invocation",
    request_factory=_redact_prompt,
)
def truth_claim_redact(
    store_id: str,
    claim_id: str,
    reason: str = "privacy",
) -> dict[str, Any]:
    """Redact one claim while retaining its hashes, links, and audit rows."""

    store = _open_store(store_id)
    _require_consent_surface(store)
    lifecycle = TruthLifecycle(store)
    with store.write_transaction() as conn:
        review = compose_claim_review(
            store,
            claim_id,
            action="redact",
            decision={"reason": reason, "subject_kind": "claim"},
        )
        authorization = _assert_authorized_review(review)
        gesture = lifecycle.mint_gesture(
            subject_ref=claim_id,
            actor=_HUMAN,
            surface=_CONSENT_SURFACE,
            kind="redact",
            displayed_payload_sha256=review.payload_sha256,
            context_sha256=review.context_sha256,
            conn=conn,
        )
        result = TruthRedactor(store).redact(
            subject_kind="claim",
            subject_ref=claim_id,
            actor=_HUMAN,
            reason=reason,
            basis_kind="gesture",
            basis_ref=gesture.id,
            expected_context_sha256=review.context_sha256,
            conn=conn,
        )
    emission = (
        emit_truth_event(
            "truth.claim_redacted",
            store_id=store.store_id,
            subject_kind="claim",
            subject_id=claim_id,
            data={"reason": reason, "gesture_id": gesture.id},
        )
        if result.created
        else None
    )
    return {
        "ok": True,
        "result": _serialize(result),
        "authorization": {
            "request_id": authorization.request_id,
            "response_surface": authorization.response_surface,
        },
        "event": _event_result(emission),
    }


def truth_query(
    store_id: str,
    view: str = "current",
    belief_at: str | None = None,
    valid_at: str | None = None,
    scope: str | None = None,
    claim_kind: str | None = None,
    include_needs_review: bool = False,
    claim_id: str | None = None,
) -> dict[str, Any]:
    """Query current, historical, conflict, or review state."""

    store = _open_store(store_id)
    normalized = str(view).strip().lower().replace("_", "-")
    if normalized not in {"current", "as-of", "conflicts", "needs-review"}:
        raise InvariantViolation(
            "view must be current, as-of, conflicts, or needs-review"
        )
    if normalized == "as-of" and belief_at is None:
        raise InvariantViolation("as-of query requires belief_at")
    if normalized == "current" and belief_at is not None:
        raise InvariantViolation("belief_at is only valid for historical views")
    if normalized in {"conflicts", "needs-review"}:
        invalid = [
            name
            for name, value in (
                ("valid_at", valid_at),
                ("scope", scope),
                ("claim_kind", claim_kind),
                ("include_needs_review", include_needs_review),
            )
            if value
        ]
        if invalid:
            raise InvariantViolation(
                f"{', '.join(invalid)} not valid for query view {normalized}"
            )
    if normalized != "conflicts" and claim_id is not None:
        raise InvariantViolation("claim_id is only valid for the conflicts view")
    if normalized == "current":
        items = current_claims(
            store,
            valid_at=valid_at,
            scope=scope,
            claim_kind=claim_kind,
            include_needs_review=include_needs_review,
        )
    elif normalized == "as-of":
        assert belief_at is not None
        items = claims_as_of(
            store,
            belief_at=belief_at,
            valid_at=valid_at,
            scope=scope,
            claim_kind=claim_kind,
            include_needs_review=include_needs_review,
        )
    elif normalized == "conflicts":
        items = conflicts(store, claim_id=claim_id, belief_at=belief_at)
    else:
        items = needs_review(store, belief_at=belief_at)
    return {
        "ok": True,
        "view": normalized,
        "count": len(items),
        "items": _serialize(items),
    }


def _cross_store_resolver(uri: str) -> PremiseResolution:
    try:
        parsed = parse_truth_uri(uri)
        if parsed.kind != "claim":
            return PremiseResolution(False, detail="premise URI does not name a claim")
        store = _open_store(parsed.store_id)
        claim = store.get_claim(parsed.record_id)
        if claim is None:
            return PremiseResolution(False, detail="claim does not exist")
        latest = TruthLifecycle(store).latest_status(parsed.record_id)
        return PremiseResolution(
            True,
            status=None if latest is None else latest.status,
        )
    except Exception as exc:
        return PremiseResolution(False, detail=str(exc))


def truth_sweep(
    store_id: str,
    kind: str,
    claim_id: str | None = None,
    evidence_id: str | None = None,
    span_id: str | None = None,
) -> dict[str, Any]:
    """Run and record an integrity, supersession, or source sweep."""

    normalized = str(kind).strip().lower()
    if normalized not in {"integrity", "supersession", "source"}:
        raise InvariantViolation("sweep kind must be integrity, supersession, or source")
    if normalized == "integrity" and any(
        value is not None for value in (claim_id, evidence_id, span_id)
    ):
        raise InvariantViolation(
            "integrity sweep does not accept claim_id, evidence_id, or span_id"
        )
    if normalized == "supersession":
        if evidence_id is not None or span_id is not None:
            raise InvariantViolation(
                "supersession sweep does not accept evidence_id or span_id"
            )
        if claim_id is None:
            raise InvariantViolation("supersession sweep requires claim_id")
    if normalized == "source":
        if claim_id is not None:
            raise InvariantViolation("source sweep does not accept claim_id")
        if (evidence_id is None) == (span_id is None):
            raise InvariantViolation(
                "source sweep requires exactly one of evidence_id or span_id"
            )
    store = _open_store(store_id)
    params: dict[str, Any]
    detected: Sequence[Any]
    if normalized == "integrity":
        detected = integrity_findings(
            store,
            cross_store_resolver=_cross_store_resolver,
        )
        findings = [
            SweepFindingSpec(
                item.subject_kind,
                item.subject_ref,
                f"{item.code}:{item.severity}:{item.detail}",
            )
            for item in detected
        ]
        params = {}
    elif normalized == "supersession":
        assert claim_id is not None
        detected = supersession_sweep_candidates(store, claim_id)
        findings = list(detected)
        params = {"claim_id": claim_id}
    elif normalized == "source":
        detected = source_sweep_candidates(
            store,
            evidence_id=evidence_id,
            span_id=span_id,
        )
        findings = list(detected)
        params = {"evidence_id": evidence_id, "span_id": span_id}
    recorded = record_sweep(
        store,
        kind=normalized,
        findings=findings,
        params=params,
    )
    emission = emit_truth_event(
        "truth.sweep_completed",
        store_id=store.store_id,
        data={"kind": normalized, "finding_count": len(recorded.finding_ids)},
    )
    return {
        "ok": True,
        "sweep": _serialize(recorded),
        "detected": _serialize(detected),
        "event": _event_result(emission),
    }


def _register() -> None:
    register_op("op.wb.truth_store_create", truth_store_create)
    register_op("op.wb.truth_store_list", truth_store_list)
    register_op("op.wb.truth_evidence_capture", truth_evidence_capture)
    register_op("op.wb.truth_span_mark", truth_span_mark)
    register_op("op.wb.truth_claim_propose", truth_claim_propose)
    register_op("op.wb.truth_claim_confirm", truth_claim_confirm)
    register_op("op.wb.truth_claim_reject", truth_claim_reject)
    register_op("op.wb.truth_claim_challenge", truth_claim_challenge)
    register_op("op.wb.truth_claim_supersede", truth_claim_supersede)
    register_op("op.wb.truth_claim_redact", truth_claim_redact)
    register_op("op.wb.truth_query", truth_query)
    register_op("op.wb.truth_sweep", truth_sweep)


_register()


__all__ = [
    "truth_claim_challenge",
    "truth_claim_confirm",
    "truth_claim_propose",
    "truth_claim_redact",
    "truth_claim_reject",
    "truth_claim_supersede",
    "truth_evidence_capture",
    "truth_query",
    "truth_span_mark",
    "truth_store_create",
    "truth_store_list",
    "truth_sweep",
]
