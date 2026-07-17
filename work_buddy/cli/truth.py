"""Direct ``wbuddy truth`` access to one scoped truth store.

The CLI opens the engine library directly. It does not require the sidecar,
the MCP gateway, or a live network service.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict
from logging import FileHandler
from pathlib import Path
from typing import Any, Mapping

from work_buddy.truth.anchors import CompositeSelector, reanchor
from work_buddy.truth.contracts import Actor, TruthError
from work_buddy.truth.events import (
    TruthEventEmission,
    emit_truth_event,
)
from work_buddy.truth.identity import sha256_bytes
from work_buddy.truth.lifecycle import TruthLifecycle
from work_buddy.truth.locators import validate_locator
from work_buddy.truth.migrations import current_version
from work_buddy.truth.queries import (
    claims_as_of,
    conflicts,
    current_claims,
    needs_review,
)
from work_buddy.truth.review import compose_claim_review
from work_buddy.truth.store import GestureRecord, TruthStore


_IDENTITY_PLACEHOLDERS = frozenset(
    {"unknown", "none", "null", "unspecified", "n/a", "na"}
)


class TruthCliError(TruthError):
    """A CLI failure that carries a structured partial result."""

    def __init__(self, message: str, *, result: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.result = None if result is None else dict(result)


def _publish_event(event_type: str, **kwargs: Any) -> dict[str, Any]:
    """Publish best effort and always return the stable emission shape."""

    try:
        emission = emit_truth_event(event_type, **kwargs)
    except Exception as exc:
        emission = TruthEventEmission(None, False, str(exc))
    return emission.to_dict()


@contextmanager
def _quiet_console_logs(enabled: bool):
    """Keep JSON stdout free of process-global work-buddy console logs."""

    changed: list[tuple[logging.Handler, int]] = []
    if enabled:
        for handler in logging.getLogger("work_buddy").handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler,
                FileHandler,
            ):
                changed.append((handler, handler.level))
                handler.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        for handler, level in changed:
            handler.setLevel(level)


def discover_store(
    explicit: str | Path | None = None,
    *,
    cwd: str | Path | None = None,
) -> Path:
    """Return the nearest scoped ``.wb-truth`` directory."""

    if explicit is not None:
        candidate = Path(explicit).expanduser().resolve()
        sidecar = candidate if candidate.name == ".wb-truth" else candidate / ".wb-truth"
        if not (sidecar / "store.yaml").is_file():
            raise TruthError(f"truth profile does not exist: {sidecar / 'store.yaml'}")
        return sidecar

    start = Path.cwd().resolve() if cwd is None else Path(cwd).expanduser().resolve()
    if start.is_file():
        start = start.parent
    for candidate in (start, *start.parents):
        sidecar = candidate if candidate.name == ".wb-truth" else candidate / ".wb-truth"
        if (sidecar / "store.yaml").is_file():
            return sidecar
    raise TruthError(f"no truth store found from {start}. Pass --store PATH")


def _registry_class():
    from work_buddy.truth.registry import TruthStoreRegistry

    return TruthStoreRegistry


def _touch_registry(store: TruthStore, registry: Any | None = None) -> None:
    """Revalidate a registered store after direct CLI access."""

    if registry is not None:
        registry.touch(store)
        return
    _registry_class()().touch(store)


def _open_store(args: Any, *, registry: Any | None = None) -> TruthStore:
    sidecar = discover_store(getattr(args, "store", None))
    store = TruthStore.open(sidecar)
    _touch_registry(store, registry)
    return store


def _session_manifest(session_id: str) -> Mapping[str, Any]:
    from work_buddy.agent_session import list_sessions

    for manifest in list_sessions():
        if str(manifest.get("session_id") or "") == session_id:
            return manifest
        if str(manifest.get("native_session_id") or "") == session_id:
            return manifest
    return {}


def _local_human_ref() -> str:
    configured = str(os.environ.get("WORK_BUDDY_HUMAN_REF") or "").strip()
    return configured or f"local:{getpass.getuser()}"


def _agent_context() -> tuple[bool, str]:
    """Return whether a normal agent context is active and its session id."""

    wb_session = str(os.environ.get("WORK_BUDDY_SESSION_ID") or "").strip()
    codex_session = str(os.environ.get("CODEX_THREAD_ID") or "").strip()
    synthetic = not wb_session or wb_session == "wbuddy-cli"
    session_id = codex_session if synthetic else wb_session
    agent_context = bool(
        session_id
        or codex_session
        or str(os.environ.get("CLAUDE_CODE_ENTRYPOINT") or "").strip()
    )
    return agent_context, session_id


def _identity_value(value: Any) -> str:
    """Normalize one producer identity value and discard placeholders."""

    normalized = str(value or "").strip()
    return "" if normalized.casefold() in _IDENTITY_PLACEHOLDERS else normalized


def _environment_identity(names: tuple[str, ...], label: str) -> str:
    """Return one unambiguous, non-placeholder environment identity claim."""

    claims = {
        value
        for name in names
        if (value := _identity_value(os.environ.get(name)))
    }
    if len(claims) > 1:
        raise TruthError(f"conflicting environment {label} claims")
    return next(iter(claims), "")


def _actor_for_write() -> Actor:
    """Resolve durable authorship without downgrading an agent to a human."""

    agent_context, session_id = _agent_context()
    if not agent_context:
        return Actor("human", _local_human_ref())
    if not session_id:
        raise TruthError("agent write is missing a durable session identity")

    manifest = _session_manifest(session_id)
    manifest_model = _identity_value(manifest.get("model"))
    manifest_harness = _identity_value(manifest.get("harness_id"))
    environment_model = _environment_identity(
        ("WORK_BUDDY_MODEL", "CODEX_MODEL", "CLAUDE_MODEL"),
        "model",
    )
    environment_harness = _environment_identity(
        ("WORK_BUDDY_HARNESS_ID",),
        "harness",
    )
    if (
        manifest_model
        and environment_model
        and manifest_model != environment_model
    ):
        raise TruthError("environment model does not match the session manifest model")
    if (
        manifest_harness
        and environment_harness
        and manifest_harness != environment_harness
    ):
        raise TruthError(
            "environment harness does not match the session manifest harness"
        )
    model = manifest_model or environment_model
    harness = manifest_harness or environment_harness
    missing = [
        name
        for name, value in (("model", model), ("harness", harness))
        if not value
    ]
    if missing:
        raise TruthError(
            "agent write is missing producer identity fields: " + ", ".join(missing)
        )
    return Actor(
        "agent_run",
        session_id,
        {
            "model": model,
            "model_source": (
                "session_manifest" if manifest_model else "caller_asserted"
            ),
            "harness": harness,
            "surface": "cli",
            "session_id": session_id,
        },
    )


def _with_model_source(
    actor: Actor,
    meta: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Persist how an agent model identity was established."""

    durable = dict(meta or {})
    if actor.kind != "agent_run":
        return durable
    model_source = str(actor.meta["model_source"])
    if "model_source" in durable and durable["model_source"] != model_source:
        raise TruthError(
            "caller meta conflicts with authoritative actor field 'model_source'"
        )
    durable["model_source"] = model_source
    return durable


def _json_object(raw: str | None, label: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TruthError(f"{label} must be a JSON object")
    return value


def _store_payload(store: TruthStore) -> dict[str, Any]:
    return {
        "store_id": store.store_id,
        "path": str(store.paths.sidecar),
        "profile": store.profile.profile,
    }


def _emit_success(
    args: Any,
    store: TruthStore | None,
    result: Mapping[str, Any],
    human_lines: list[str],
) -> int:
    if getattr(args, "json", False):
        payload: dict[str, Any] = {"ok": True, "result": dict(result)}
        if store is not None:
            payload["store"] = _store_payload(store)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        for line in human_lines:
            print(line)
    return 0


def _emit_error(args: Any, exc: Exception) -> int:
    if getattr(args, "json", False):
        payload: dict[str, Any] = {
            "ok": False,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
        partial = getattr(exc, "result", None)
        if partial is not None:
            payload["result"] = partial
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(f"error: {exc}", file=sys.stderr)
    return 1


def _capture_content(args: Any) -> str | bytes | None:
    content = getattr(args, "content", None)
    content_file = getattr(args, "content_file", None)
    if content_file is None:
        return content
    if content_file == "-":
        return sys.stdin.buffer.read()
    return Path(content_file).expanduser().read_bytes()


def _cmd_capture(args: Any) -> tuple[TruthStore, dict[str, Any], list[str]]:
    store = _open_store(args)
    actor = _actor_for_write()
    meta = _json_object(getattr(args, "meta_json", None), "meta_json")
    content = _capture_content(args)
    quote = getattr(args, "quote", None)
    anchor_values = (
        getattr(args, "prefix", None),
        getattr(args, "suffix", None),
        getattr(args, "start", None),
        getattr(args, "end", None),
    )
    if quote is None and any(value is not None for value in anchor_values):
        raise TruthError("quote anchor fields require --quote")
    if (getattr(args, "start", None) is None) != (getattr(args, "end", None) is None):
        raise TruthError("--start and --end must be supplied together")
    selector: CompositeSelector | None = None
    if quote is not None:
        if content is None:
            raise TruthError("hash-only evidence cannot mark a quote span")
        if getattr(args, "origin", None) == "mixed_transcript":
            raise TruthError(
                "mixed evidence quote spans require an authorship-aware surface"
            )
        selector = CompositeSelector(
            exact=quote,
            prefix=getattr(args, "prefix", None) or "",
            suffix=getattr(args, "suffix", None) or "",
            start=getattr(args, "start", None),
            end=getattr(args, "end", None),
        )
        if isinstance(content, str):
            anchor_text = content
        else:
            try:
                anchor_text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise TruthError("quoted evidence content must be valid UTF-8") from exc
        reanchor(anchor_text, selector)

    supplied_digest = getattr(args, "content_sha256", None)
    if supplied_digest is None and content is not None:
        raw_content = content.encode("utf-8") if isinstance(content, str) else content
        supplied_digest = sha256_bytes(raw_content)
    locator = validate_locator(
        args.kind,
        args.source_locator,
        meta,
        supplied_digest,
    )
    normalized_meta = _with_model_source(actor, locator.meta)
    normalized_meta["verifiability_class"] = locator.verifiability_class
    normalized_meta["integrity_recipe"] = dict(locator.integrity_recipe)

    evidence = store.capture_evidence(
        kind=args.kind,
        source_locator=locator.locator,
        actor=actor,
        acquisition_method=args.acquisition_method,
        content=content,
        content_sha256=locator.content_sha256,
        media_type=getattr(args, "media_type", None),
        acquired_at=getattr(args, "acquired_at", None),
        origin=getattr(args, "origin", None),
        external_reviewed=getattr(args, "external_reviewed", False),
        derived_from_store=getattr(args, "derived_from_store", None),
        meta=normalized_meta,
    )

    span = None
    if selector is not None:
        span = store.mark_span(
            evidence_id=evidence.id,
            selector=selector,
            actor=actor,
        )
    events = [
        _publish_event(
            "truth.evidence_captured",
            store_id=store.store_id,
            subject_kind="evidence",
            subject_id=evidence.id,
            data={
                "kind": evidence.kind,
                "source_locator": evidence.source_locator,
                "content_sha256": evidence.content_sha256,
                "trust_class": evidence.trust_class,
            },
        )
    ]
    if span is not None:
        events.append(
            _publish_event(
                "truth.span_marked",
                store_id=store.store_id,
                subject_kind="span",
                subject_id=span.id,
                data={
                    "evidence_id": evidence.id,
                    "span_sha256": span.span_sha256,
                },
            )
        )

    result = {
        "evidence_id": evidence.id,
        "content_sha256": evidence.content_sha256,
        "trust_class": evidence.trust_class,
        "span_id": None if span is None else span.id,
        "events": events,
    }
    lines = [
        f"Captured evidence {evidence.id} ({evidence.trust_class}).",
    ]
    if span is not None:
        lines.append(f"Marked span {span.id}.")
    return store, result, lines


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


def _cmd_propose(args: Any) -> tuple[TruthStore, dict[str, Any], list[str]]:
    store = _open_store(args)
    actor = _actor_for_write()
    structured = _json_object(
        getattr(args, "structured_json", None),
        "structured_json",
    )
    meta = _json_object(getattr(args, "meta_json", None), "meta_json")
    support_ids: list[str] = []
    with store.write_transaction() as conn:
        written = store.propose_claim(
            proposition=args.proposition,
            claim_kind=args.kind,
            actor=actor,
            structured=structured,
            scope=getattr(args, "scope", "store"),
            valid_from=getattr(args, "valid_from", None),
            valid_to=getattr(args, "valid_to", None),
            confidence_extraction=getattr(args, "confidence", None),
            meta=_with_model_source(actor, meta),
            conn=conn,
        )
        for span_id in getattr(args, "support_span", None) or []:
            existing = _existing_support_link(conn, written.claim.id, span_id)
            if existing is not None:
                support_ids.append(existing)
                continue
            link = store.add_link(
                from_claim_id=written.claim.id,
                link_type="supports_span",
                to_kind="evidence_span",
                to_ref=span_id,
                actor=actor,
                conn=conn,
            )
            support_ids.append(link.id)

    events: list[dict[str, Any]] = []
    if written.created:
        events.append(
            _publish_event(
                "truth.claim_proposed",
                store_id=store.store_id,
                subject_kind="claim",
                subject_id=written.claim.id,
                data={
                    "canonical_sha256": written.claim.canonical_sha256,
                    "claim_kind": written.claim.claim_kind,
                    "scope": written.claim.scope,
                    "support_link_ids": support_ids,
                },
            )
        )
    result = {
        "claim_id": written.claim.id,
        "canonical_sha256": written.claim.canonical_sha256,
        "status": "proposed",
        "created": written.created,
        "support_link_ids": support_ids,
        "events": events,
    }
    action = "Proposed" if written.created else "Found existing"
    return store, result, [f"{action} claim {written.claim.id}."]


def _claim_state_payload(state: Any) -> dict[str, Any]:
    return {
        "claim_id": state.claim.id,
        "proposition": state.claim.proposition,
        "canonical_sha256": state.claim.canonical_sha256,
        "claim_kind": state.claim.claim_kind,
        "scope": state.claim.scope,
        "base_status": state.base_status,
        "status": state.status,
        "needs_review": state.needs_review,
        "effective_valid_from": state.effective_valid_from,
        "effective_valid_to": state.effective_valid_to,
        "health": state.health,
        "health_reason": state.health_reason,
    }


def _validate_query_flags(args: Any) -> None:
    view = args.view
    belief_at = getattr(args, "belief_at", None)
    if view == "as-of" and belief_at is None:
        raise TruthError("query --view as-of requires --belief-at")
    if view == "current" and belief_at is not None:
        raise TruthError("--belief-at is only valid for as-of, needs-review, or conflicts")
    if view in {"needs-review", "conflicts"}:
        invalid = [
            flag
            for flag, value in (
                ("--valid-at", getattr(args, "valid_at", None)),
                ("--scope", getattr(args, "scope", None)),
                ("--claim-kind", getattr(args, "claim_kind", None)),
                (
                    "--include-needs-review",
                    getattr(args, "include_needs_review", False),
                ),
            )
            if value
        ]
        if invalid:
            raise TruthError(f"{', '.join(invalid)} not valid for query view {view}")
    if view != "conflicts" and getattr(args, "claim_id", None) is not None:
        raise TruthError("--claim-id is only valid for query view conflicts")


def _cmd_query(args: Any) -> tuple[TruthStore, dict[str, Any], list[str]]:
    _validate_query_flags(args)
    store = _open_store(args)
    if args.view == "current":
        items = [
            _claim_state_payload(item)
            for item in current_claims(
                store,
                valid_at=getattr(args, "valid_at", None),
                scope=getattr(args, "scope", None),
                claim_kind=getattr(args, "claim_kind", None),
                include_needs_review=getattr(args, "include_needs_review", False),
            )
        ]
    elif args.view == "as-of":
        items = [
            _claim_state_payload(item)
            for item in claims_as_of(
                store,
                belief_at=args.belief_at,
                valid_at=getattr(args, "valid_at", None),
                scope=getattr(args, "scope", None),
                claim_kind=getattr(args, "claim_kind", None),
                include_needs_review=getattr(args, "include_needs_review", False),
            )
        ]
    elif args.view == "needs-review":
        items = [
            asdict(item)
            for item in needs_review(store, belief_at=getattr(args, "belief_at", None))
        ]
    else:
        items = [
            asdict(item)
            for item in conflicts(
                store,
                claim_id=getattr(args, "claim_id", None),
                belief_at=getattr(args, "belief_at", None),
            )
        ]
    result = {
        "view": args.view,
        "count": len(items),
        "items": items,
        "events": [],
    }
    lines = [f"{args.view}: {len(items)} item(s)"]
    for item in items:
        identifier = item.get("claim_id") or item.get("subject_ref") or item.get("link_id")
        detail = item.get("proposition") or item.get("status") or item.get("findings")
        lines.append(f"  {identifier}: {detail}")
    return store, result, lines


def _is_interactive_tty() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _prompt_confirmation(body: str, *, json_mode: bool) -> bool:
    stream = sys.stderr if json_mode else sys.stdout
    print(body, file=stream)
    print("Confirm this claim? [y/N] ", end="", file=stream, flush=True)
    response = sys.stdin.readline()
    return response.strip().lower() in {"y", "yes"}


def _gesture(store: TruthStore, gesture_id: str) -> GestureRecord:
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT * FROM gestures WHERE id = ?",
            (gesture_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise TruthError(f"gesture does not exist: {gesture_id}")
    return GestureRecord(**dict(row))


def _cmd_confirm(args: Any) -> tuple[TruthStore, dict[str, Any], list[str]]:
    store = _open_store(args)
    claim = store.get_claim(args.claim_id)
    if claim is None:
        raise TruthError(f"claim does not exist: {args.claim_id}")
    lifecycle = TruthLifecycle(store)
    gesture_id = getattr(args, "gesture", None)

    if gesture_id is None:
        agent_context, _ = _agent_context()
        if agent_context:
            raise TruthError(
                "agent sessions cannot use interactive CLI confirmation. "
                "Use MCP per-invocation consent or --gesture <id> minted by a human"
            )
        if not _is_interactive_tty():
            raise TruthError("confirmation requires an interactive TTY or --gesture <id>")
        review = compose_claim_review(store, claim.id, action="confirm")
        if not _prompt_confirmation(
            review.body,
            json_mode=getattr(args, "json", False),
        ):
            raise TruthError("claim was not confirmed")
        actor = Actor("human", _local_human_ref())
        with store.write_transaction() as conn:
            fresh_review = compose_claim_review(store, claim.id, action="confirm")
            if fresh_review.request_fingerprint != review.request_fingerprint:
                raise TruthError(
                    "claim review changed while awaiting confirmation. Review it again"
                )
            gesture = lifecycle.mint_gesture(
                subject_ref=claim.id,
                actor=actor,
                surface="cli",
                kind="confirm",
                displayed_payload_sha256=fresh_review.payload_sha256,
                context_sha256=fresh_review.context_sha256,
                expires_at=None,
                conn=conn,
            )
            confirmed = lifecycle.confirm_claim(
                claim_id=claim.id,
                gesture_id=gesture.id,
                actor=actor,
                expected_context_sha256=fresh_review.context_sha256,
                conn=conn,
            )
    else:
        gesture = _gesture(store, gesture_id)
        actor = Actor("human", gesture.actor_ref)
        with store.write_transaction() as conn:
            fresh_review = compose_claim_review(store, claim.id, action="confirm")
            confirmed = lifecycle.confirm_claim(
                claim_id=claim.id,
                gesture_id=gesture.id,
                actor=actor,
                expected_context_sha256=fresh_review.context_sha256,
                conn=conn,
            )

    status = "needs_review" if confirmed.needs_review_event is not None else "confirmed"
    events: list[dict[str, Any]] = []
    if confirmed.created:
        events.append(
            _publish_event(
                "truth.claim_confirmed",
                store_id=store.store_id,
                subject_kind="claim",
                subject_id=claim.id,
                data={
                    "status": status,
                    "status_event_id": (
                        None if confirmed.event is None else confirmed.event.id
                    ),
                    "gesture_id": confirmed.gesture.id,
                    "superseded": [
                        {
                            "claim_id": event.claim_id,
                            "status_event_id": event.id,
                        }
                        for event in confirmed.superseded_events
                    ],
                    "needs_review_event_id": (
                        None
                        if confirmed.needs_review_event is None
                        else confirmed.needs_review_event.id
                    ),
                },
            )
        )
    result = {
        "claim_id": claim.id,
        "status": status,
        "created": confirmed.created,
        "gesture_id": confirmed.gesture.id,
        "superseded_claim_ids": [
            event.claim_id for event in confirmed.superseded_events
        ],
        "events": events,
    }
    return store, result, [f"Claim {claim.id}: {status}."]


def _schema_version(store: TruthStore) -> int:
    conn = store.connect()
    try:
        return current_version(conn)
    finally:
        conn.close()


def _row_value(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _cmd_migrate(args: Any) -> tuple[TruthStore | None, dict[str, Any], list[str]]:
    if not getattr(args, "all_stores", False):
        store = _open_store(args)
        version = _schema_version(store)
        result = {
            "stores": [
                {
                    **_store_payload(store),
                    "schema_version": version,
                    "status": "ok",
                }
            ],
            "failed": 0,
            "events": [],
        }
        return store, result, [f"Migrated {store.paths.sidecar} to schema {version}."]

    if getattr(args, "store", None) is not None:
        raise TruthError("migrate --all cannot be combined with --store")
    registry = _registry_class()()
    rows = registry.list_stores(refresh=True)
    results: list[dict[str, Any]] = []
    failed = 0
    for row in rows:
        path = _row_value(row, "path")
        reachable = bool(_row_value(row, "reachable", False))
        if not reachable:
            failed += 1
            results.append({"path": str(path), "status": "unreachable"})
            continue
        try:
            store = TruthStore.open(path)
            registry.touch(store)
            results.append(
                {
                    **_store_payload(store),
                    "schema_version": _schema_version(store),
                    "status": "ok",
                }
            )
        except Exception as exc:
            failed += 1
            results.append(
                {
                    "path": str(path),
                    "status": "error",
                    "error": str(exc),
                }
            )
    result = {"stores": results, "failed": failed, "events": []}
    lines = [
        f"Migration sweep: {len(results) - failed} ok, {failed} failed.",
    ]
    if failed:
        details = ", ".join(
            f"{item.get('path', '<unknown>')}={item['status']}"
            for item in results
            if item["status"] != "ok"
        )
        raise TruthCliError(
            f"migration sweep incomplete: {failed} store(s) failed. {details}",
            result=result,
        )
    return None, result, lines


def cmd_truth(args: Any) -> int:
    """Run one direct truth-store verb and preserve the CLI error contract."""

    try:
        with _quiet_console_logs(getattr(args, "json", False)):
            command = getattr(args, "truth_command", None)
            if command == "capture":
                store, result, lines = _cmd_capture(args)
            elif command == "propose":
                store, result, lines = _cmd_propose(args)
            elif command == "query":
                store, result, lines = _cmd_query(args)
            elif command == "confirm":
                store, result, lines = _cmd_confirm(args)
            elif command == "migrate":
                store, result, lines = _cmd_migrate(args)
            else:
                raise TruthError(f"unknown truth command: {command}")
    except Exception as exc:
        return _emit_error(args, exc)
    return _emit_success(args, store, result, lines)


__all__ = ["cmd_truth", "discover_store"]
