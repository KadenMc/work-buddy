from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from flask import Flask, request

from work_buddy.dashboard import local_identity_api
from work_buddy.dashboard.local_identity_launch import (
    bootstrap_fragment_for_dashboard,
)
from work_buddy.security.local_identity import (
    DEFAULT_AUDIENCE,
    HUMAN_AUTHORITY_THREAT_LIMIT,
    BoundaryRequest,
    LocalIdentityAuthority,
    LocalIdentityError,
    LocalIdentityPolicy,
    SESSION_COOKIE_NAME,
)
from work_buddy.security.actors import ActorRef, InvalidActorReference


class Clock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _policy() -> LocalIdentityPolicy:
    return LocalIdentityPolicy(
        bootstrap_ttl_seconds=10,
        session_idle_ttl_seconds=120,
        session_absolute_ttl_seconds=300,
        session_rotation_seconds=60,
        gesture_ttl_seconds=10,
    )


def _boundary(
    *,
    origin: str | None = "http://127.0.0.1:5127",
    remote_addr: str = "127.0.0.1",
    host: str = "127.0.0.1:5127",
    proxy_marked: bool = False,
) -> BoundaryRequest:
    return BoundaryRequest(
        remote_addr=remote_addr,
        scheme="http",
        host=host,
        origin=origin,
        proxy_marked=proxy_marked,
    )


@pytest.fixture
def authority(tmp_path: Path) -> tuple[LocalIdentityAuthority, Clock]:
    clock = Clock()
    return (
        LocalIdentityAuthority(
            tmp_path / "local-identity.db", policy=_policy(), clock=clock
        ),
        clock,
    )


def _session(
    authority: LocalIdentityAuthority,
    *,
    boundary: BoundaryRequest | None = None,
):
    selected = boundary or _boundary()
    bootstrap = authority.mint_bootstrap(
        origin="http://127.0.0.1:5127",
        audience=DEFAULT_AUDIENCE,
    )
    return authority.redeem_bootstrap(
        token=bootstrap.token,
        boundary=selected,
        audience=DEFAULT_AUDIENCE,
    )


def test_enrollment_is_persistent_and_authority_qualified(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "identity.db"
    first = LocalIdentityAuthority(db_path).enrolled_actor()
    second = LocalIdentityAuthority(db_path).enrolled_actor()

    assert first == second
    assert first.schema == "wb.actor-ref/v1"
    assert first.kind == "human"
    assert first.issuer_authority_id.startswith("wia_")
    assert first.tenant_scope_id.startswith("wts_")
    assert first.subject.startswith("wactor_")
    assert first.subject != "dashboard-user"


def test_actor_ref_round_trip_is_strict_and_deterministic() -> None:
    actor = ActorRef(
        issuer_authority_id="authority-00000001",
        subject="profile-00000001",
        kind="human",
        tenant_scope_id="tenant-00000001",
    )
    assert ActorRef.from_dict(actor.to_dict()) == actor
    assert actor.canonical_id == (
        '{"issuer_authority_id":"authority-00000001","kind":"human",'
        '"schema":"wb.actor-ref/v1","subject":"profile-00000001",'
        '"tenant_scope_id":"tenant-00000001"}'
    )
    with pytest.raises(InvalidActorReference):
        ActorRef.from_dict({**actor.to_dict(), "unexpected": "field"})
    with pytest.raises(InvalidActorReference):
        ActorRef(
            issuer_authority_id="too-short",
            subject="profile-00000001",
            kind="human",
            tenant_scope_id="tenant-00000001",
            schema="wb.actor-ref/v2",
        )


def test_bootstrap_is_exact_origin_audience_single_use_and_short_lived(
    authority: tuple[LocalIdentityAuthority, Clock],
) -> None:
    service, clock = authority
    grant = service.mint_bootstrap(
        origin="http://127.0.0.1:5127", audience=DEFAULT_AUDIENCE
    )

    with pytest.raises(LocalIdentityError, match="different Origin") as mismatch:
        service.redeem_bootstrap(
            token=grant.token,
            boundary=_boundary(
                origin="http://localhost:5127", host="localhost:5127"
            ),
        )
    assert mismatch.value.code == "origin_mismatch"

    session = service.redeem_bootstrap(token=grant.token, boundary=_boundary())
    assert session.principal.actor == service.enrolled_actor()

    with pytest.raises(LocalIdentityError) as replay:
        service.redeem_bootstrap(token=grant.token, boundary=_boundary())
    assert replay.value.code == "bootstrap_replayed"

    expired = service.mint_bootstrap(origin="http://127.0.0.1:5127")
    clock.advance(11)
    with pytest.raises(LocalIdentityError) as expiry:
        service.redeem_bootstrap(token=expired.token, boundary=_boundary())
    assert expiry.value.code == "bootstrap_expired"


@pytest.mark.parametrize(
    ("boundary", "code"),
    [
        (_boundary(remote_addr="100.64.0.8"), "loopback_required"),
        (_boundary(proxy_marked=True), "direct_loopback_required"),
        (_boundary(origin=None), "origin_required"),
    ],
)
def test_human_session_rejects_remote_proxy_and_originless_bootstrap(
    authority: tuple[LocalIdentityAuthority, Clock],
    boundary: BoundaryRequest,
    code: str,
) -> None:
    service, _ = authority
    grant = service.mint_bootstrap(origin="http://127.0.0.1:5127")
    with pytest.raises(LocalIdentityError) as failure:
        service.redeem_bootstrap(token=grant.token, boundary=boundary)
    assert failure.value.code == code


def test_csrf_rotation_revocation_and_expiry(
    authority: tuple[LocalIdentityAuthority, Clock],
) -> None:
    service, clock = authority
    session = _session(service)

    with pytest.raises(LocalIdentityError) as csrf:
        service.authenticate_session(
            cookie_token=session.cookie_token,
            csrf_token="wbc_" + "x" * 43,
            require_csrf=True,
            boundary=_boundary(),
        )
    assert csrf.value.code == "csrf_mismatch"

    principal = service.authenticate_session(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        require_csrf=True,
        boundary=_boundary(),
    )
    assert principal.actor == service.enrolled_actor()

    clock.advance(61)
    with pytest.raises(LocalIdentityError) as rotation_required:
        service.authenticate_session(
            cookie_token=session.cookie_token,
            csrf_token=session.csrf_token,
            require_csrf=True,
            boundary=_boundary(),
        )
    assert rotation_required.value.code == "session_rotation_required"

    rotated = service.rotate_session(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
    )
    with pytest.raises(LocalIdentityError) as old_session:
        service.authenticate_session(
            cookie_token=session.cookie_token, boundary=_boundary()
        )
    assert old_session.value.code == "session_unavailable"

    service.revoke_session(
        cookie_token=rotated.cookie_token,
        csrf_token=rotated.csrf_token,
        boundary=_boundary(),
    )
    with pytest.raises(LocalIdentityError) as revoked:
        service.authenticate_session(
            cookie_token=rotated.cookie_token, boundary=_boundary()
        )
    assert revoked.value.code == "session_unavailable"


def test_session_and_gesture_expiry_are_enforced(
    authority: tuple[LocalIdentityAuthority, Clock],
) -> None:
    service, clock = authority
    session = _session(service)
    digest = hashlib.sha256(b"context").hexdigest()
    _, gesture = service.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action="sources.capture",
        subject="journal:quick-capture",
        context_sha256=digest,
    )
    clock.advance(11)
    with pytest.raises(LocalIdentityError) as expired_gesture:
        service.authorize_human_mutation(
            cookie_token=session.cookie_token,
            csrf_token=session.csrf_token,
            gesture_token=gesture.token,
            boundary=_boundary(),
            action="sources.capture",
            subject="journal:quick-capture",
            context_sha256=digest,
        )
    assert expired_gesture.value.code == "gesture_expired"

    clock.advance(110)
    with pytest.raises(LocalIdentityError) as expired_session:
        service.authenticate_session(
            cookie_token=session.cookie_token,
            boundary=_boundary(),
            allow_rotation_due=True,
        )
    assert expired_session.value.code == "session_expired"


def test_raw_bearer_credentials_are_not_persisted(
    authority: tuple[LocalIdentityAuthority, Clock],
) -> None:
    service, _ = authority
    bootstrap = service.mint_bootstrap(origin="http://127.0.0.1:5127")
    session = service.redeem_bootstrap(
        token=bootstrap.token, boundary=_boundary()
    )
    digest = hashlib.sha256(b"context").hexdigest()
    _, gesture = service.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action="sources.capture",
        subject="journal:quick-capture",
        context_sha256=digest,
    )

    with sqlite3.connect(service.db_path) as conn:
        sql_dump = "\n".join(conn.iterdump())
    assert bootstrap.token not in sql_dump
    assert session.cookie_token not in sql_dump
    assert session.csrf_token not in sql_dump
    assert gesture.token not in sql_dump


def test_csrf_can_be_recovered_after_reload_only_at_bound_origin(
    authority: tuple[LocalIdentityAuthority, Clock],
) -> None:
    service, _ = authority
    session = _session(service)
    principal, csrf = service.refresh_csrf(
        cookie_token=session.cookie_token, boundary=_boundary()
    )
    assert principal.actor == service.enrolled_actor()
    assert csrf != session.csrf_token
    # A reload in one tab must not invalidate another open tab's in-memory
    # token.  The server retains a bounded set of hashed tokens per session.
    service.authenticate_session(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        require_csrf=True,
        boundary=_boundary(),
    )

    with pytest.raises(LocalIdentityError) as remote:
        service.refresh_csrf(
            cookie_token=session.cookie_token,
            boundary=_boundary(remote_addr="100.64.0.8"),
        )
    assert remote.value.code == "loopback_required"


def test_gesture_is_single_use_and_bound_to_session_action_subject_and_context(
    authority: tuple[LocalIdentityAuthority, Clock],
) -> None:
    service, _ = authority
    session = _session(service)
    context_digest = hashlib.sha256(b"exact mutation context").hexdigest()
    _, gesture = service.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action="truth.claim.confirm",
        subject="truth://store/claim/1",
        context_sha256=context_digest,
    )

    with pytest.raises(LocalIdentityError) as mismatch:
        service.authorize_human_mutation(
            cookie_token=session.cookie_token,
            csrf_token=session.csrf_token,
            gesture_token=gesture.token,
            boundary=_boundary(),
            action="truth.claim.reject",
            subject="truth://store/claim/1",
            context_sha256=context_digest,
        )
    assert mismatch.value.code == "gesture_binding_mismatch"

    authorized = service.authorize_human_mutation(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        gesture_token=gesture.token,
        boundary=_boundary(),
        action="truth.claim.confirm",
        subject="truth://store/claim/1",
        context_sha256=context_digest,
    )
    assert authorized.principal.actor == service.enrolled_actor()
    assert authorized.assurance == "enrolled_local_session_gesture"
    assert authorized.threat_model_limit == HUMAN_AUTHORITY_THREAT_LIMIT
    assert "does not prove physical presence" in authorized.threat_model_limit

    with pytest.raises(LocalIdentityError) as replay:
        service.authorize_human_mutation(
            cookie_token=session.cookie_token,
            csrf_token=session.csrf_token,
            gesture_token=gesture.token,
            boundary=_boundary(),
            action="truth.claim.confirm",
            subject="truth://store/claim/1",
            context_sha256=context_digest,
        )
    assert replay.value.code == "gesture_replayed"


def test_gesture_cannot_cross_sessions(
    authority: tuple[LocalIdentityAuthority, Clock],
) -> None:
    service, _ = authority
    first = _session(service)
    second = _session(service)
    context_digest = hashlib.sha256(b"context").hexdigest()
    _, gesture = service.issue_gesture(
        cookie_token=first.cookie_token,
        csrf_token=first.csrf_token,
        boundary=_boundary(),
        action="sources.capture",
        subject="journal:quick-capture",
        context_sha256=context_digest,
    )
    with pytest.raises(LocalIdentityError) as mismatch:
        service.authorize_human_mutation(
            cookie_token=second.cookie_token,
            csrf_token=second.csrf_token,
            gesture_token=gesture.token,
            boundary=_boundary(),
            action="sources.capture",
            subject="journal:quick-capture",
            context_sha256=context_digest,
        )
    assert mismatch.value.code == "gesture_binding_mismatch"


def test_human_authority_mutation_fails_closed_off_loopback(
    authority: tuple[LocalIdentityAuthority, Clock],
) -> None:
    service, _ = authority
    session = _session(service)
    context_digest = hashlib.sha256(b"context").hexdigest()
    _, gesture = service.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action="truth.claim.confirm",
        subject="claim:1",
        context_sha256=context_digest,
    )
    with pytest.raises(LocalIdentityError) as remote:
        service.authorize_human_mutation(
            cookie_token=session.cookie_token,
            csrf_token=session.csrf_token,
            gesture_token=gesture.token,
            boundary=_boundary(remote_addr="100.64.0.8"),
            action="truth.claim.confirm",
            subject="claim:1",
            context_sha256=context_digest,
        )
    assert remote.value.code == "loopback_required"


def test_dashboard_routes_ignore_caller_actor_fields_and_set_strict_cookie(
    authority: tuple[LocalIdentityAuthority, Clock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = authority
    monkeypatch.setattr(local_identity_api, "_authority", lambda: service)
    app = Flask("local-identity-test")
    local_identity_api.register_routes(app)
    client = app.test_client()

    bootstrap = service.mint_bootstrap(origin="http://127.0.0.1:5127")
    response = client.post(
        "/api/local-identity/bootstrap/redeem",
        json={
            "token": bootstrap.token,
            "audience": DEFAULT_AUDIENCE,
            "actor": {"subject": "attacker-selected"},
        },
        headers={
            "Origin": "http://127.0.0.1:5127",
            "Host": "127.0.0.1:5127",
            "X-WB-User-Ref": "attacker-selected",
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert response.status_code == 200
    assert response.json["principal"]["actor"] == service.enrolled_actor().to_dict()
    assert response.json["principal"]["actor"]["subject"] != "attacker-selected"
    cookie = response.headers["Set-Cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Path=/" in cookie
    assert "Cache-Control" in response.headers
    # The HTTP surface can redeem host-minted grants but can never mint one.
    assert client.post("/api/local-identity/bootstrap").status_code == 404

    csrf = response.json["csrf_token"]
    context_digest = hashlib.sha256(b"context").hexdigest()
    gesture_response = client.post(
        "/api/local-identity/gestures",
        json={
            "action": "truth.claim.confirm",
            "subject": "claim:1",
            "context_sha256": context_digest,
            "actor_ref": "another-attacker-value",
        },
        headers={
            "Origin": "http://127.0.0.1:5127",
            "Host": "127.0.0.1:5127",
            "X-WB-CSRF": csrf,
            "X-WB-User-Ref": "another-attacker-value",
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert gesture_response.status_code == 200
    assert (
        gesture_response.json["principal"]["actor"]["subject"]
        == service.enrolled_actor().subject
    )


@pytest.mark.parametrize("session_state", ["missing", "unavailable", "expired"])
def test_csrf_recovery_treats_absent_session_as_normal_unauthenticated_state(
    authority: tuple[LocalIdentityAuthority, Clock],
    monkeypatch: pytest.MonkeyPatch,
    session_state: str,
) -> None:
    service, clock = authority
    monkeypatch.setattr(local_identity_api, "_authority", lambda: service)
    app = Flask(f"local-identity-csrf-{session_state}")
    local_identity_api.register_routes(app)
    client = app.test_client()

    if session_state == "unavailable":
        client.set_cookie(
            SESSION_COOKIE_NAME,
            "wbs_" + "x" * 43,
            domain="127.0.0.1",
        )
    elif session_state == "expired":
        session = _session(service)
        client.set_cookie(
            SESSION_COOKIE_NAME,
            session.cookie_token,
            domain="127.0.0.1",
        )
        clock.advance(_policy().session_idle_ttl_seconds + 1)

    response = client.post(
        "/api/local-identity/session/csrf",
        headers={
            "Origin": "http://127.0.0.1:5127",
            "Host": "127.0.0.1:5127",
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert response.json == {
        "ok": True,
        "authenticated": False,
        "human_authority_available": False,
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Set-Cookie"].startswith(f"{SESSION_COOKIE_NAME}=;")


@pytest.mark.parametrize(
    ("headers", "remote_addr", "code"),
    [
        (
            {"Origin": "http://127.0.0.1:5127", "Host": "127.0.0.1:5127"},
            "100.64.0.8",
            "loopback_required",
        ),
        (
            {"Host": "127.0.0.1:5127"},
            "127.0.0.1",
            "origin_required",
        ),
        (
            {"Origin": "http://localhost:5127", "Host": "127.0.0.1:5127"},
            "127.0.0.1",
            "origin_mismatch",
        ),
        (
            {
                "Origin": "http://127.0.0.1:5127",
                "Host": "127.0.0.1:5127",
                "X-Forwarded-For": "127.0.0.1",
            },
            "127.0.0.1",
            "direct_loopback_required",
        ),
    ],
)
def test_csrf_recovery_preserves_transport_security_failures(
    authority: tuple[LocalIdentityAuthority, Clock],
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    remote_addr: str,
    code: str,
) -> None:
    service, _ = authority
    monkeypatch.setattr(local_identity_api, "_authority", lambda: service)
    app = Flask(f"local-identity-csrf-security-{code}")
    local_identity_api.register_routes(app)
    client = app.test_client()
    session = _session(service)
    client.set_cookie(
        SESSION_COOKIE_NAME,
        session.cookie_token,
        domain="127.0.0.1",
    )

    response = client.post(
        "/api/local-identity/session/csrf",
        headers=headers,
        environ_base={"REMOTE_ADDR": remote_addr},
    )

    assert response.status_code == 403
    assert response.json["error"]["code"] == code


def test_csrf_recovery_preserves_malformed_credential_failure(
    authority: tuple[LocalIdentityAuthority, Clock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = authority
    monkeypatch.setattr(local_identity_api, "_authority", lambda: service)
    app = Flask("local-identity-csrf-invalid-credential")
    local_identity_api.register_routes(app)
    client = app.test_client()
    client.set_cookie(SESSION_COOKIE_NAME, "not-a-session", domain="127.0.0.1")

    response = client.post(
        "/api/local-identity/session/csrf",
        headers={
            "Origin": "http://127.0.0.1:5127",
            "Host": "127.0.0.1:5127",
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 401
    assert response.json["error"]["code"] == "invalid_credential"


def test_dashboard_human_authority_helper_fails_closed_remotely(
    authority: tuple[LocalIdentityAuthority, Clock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = authority
    monkeypatch.setattr(local_identity_api, "_authority", lambda: service)
    app = Flask("local-identity-remote-test")
    context_digest = hashlib.sha256(b"context").hexdigest()
    session = _session(service)
    _, gesture = service.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action="truth.claim.confirm",
        subject="claim:1",
        context_sha256=context_digest,
    )

    @app.post("/protected")
    def protected():
        try:
            local_identity_api.require_human_authority_request(
                action="truth.claim.confirm",
                subject="claim:1",
                context_sha256=context_digest,
            )
        except LocalIdentityError as exc:
            return {"ok": False, "code": exc.code}, exc.status
        return {"ok": True}, 200

    client = app.test_client()
    client.set_cookie(
        SESSION_COOKIE_NAME, session.cookie_token, domain="127.0.0.1"
    )
    response = client.post(
        "/protected",
        headers={
            "Origin": "http://127.0.0.1:5127",
            "Host": "127.0.0.1:5127",
            "X-WB-CSRF": session.csrf_token,
            "X-WB-Gesture": gesture.token,
            "X-WB-User-Ref": "attacker-selected",
        },
        environ_base={"REMOTE_ADDR": "100.64.0.8"},
    )
    assert response.status_code == 403
    assert response.json == {"ok": False, "code": "loopback_required"}


def test_cowork_action_rejects_absent_mismatched_replayed_and_remote_gestures(
    authority: tuple[LocalIdentityAuthority, Clock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = authority
    monkeypatch.setattr(local_identity_api, "_authority", lambda: service)
    from work_buddy.cowork import api as cowork_api

    app = Flask("cowork-local-authority-test")
    body = {"criterion_key": "positive-definition", "enabled": True}

    @app.post("/cowork")
    def protected_cowork_action():
        try:
            _authority, actor = cowork_api._require_human_action(
                operation="verify.criterion_update",
                store_id="store-1",
                document_id="document-1",
                body=request.get_json(),
            )
        except LocalIdentityError as exc:
            return {"ok": False, "code": exc.code}, exc.status
        return {"ok": True, "actor": actor.ref}, 200

    session = _session(service)
    client = app.test_client()
    client.set_cookie(
        SESSION_COOKIE_NAME,
        session.cookie_token,
        domain="127.0.0.1",
    )
    base_headers = {
        "Origin": "http://127.0.0.1:5127",
        "Host": "127.0.0.1:5127",
        "X-WB-CSRF": session.csrf_token,
        "X-WB-User-Ref": "attacker-selected",
    }

    absent = client.post("/cowork", json=body, headers=base_headers)
    assert absent.status_code == 403
    assert absent.json["code"] == "gesture_required"

    action = "cowork.verify.criterion_update"
    subject = "cowork-document:store-1:document-1"
    exact_context = cowork_api.cowork_mutation_context_sha256(
        operation="verify.criterion_update",
        store_id="store-1",
        document_id="document-1",
        body=body,
    )
    _, mismatched_gesture = service.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action=action,
        subject=subject,
        context_sha256=hashlib.sha256(b"different body").hexdigest(),
    )
    mismatch = client.post(
        "/cowork",
        json=body,
        headers={**base_headers, "X-WB-Gesture": mismatched_gesture.token},
    )
    assert mismatch.status_code == 409
    assert mismatch.json["code"] == "gesture_binding_mismatch"

    _, gesture = service.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action=action,
        subject=subject,
        context_sha256=exact_context,
    )
    authorized_headers = {**base_headers, "X-WB-Gesture": gesture.token}
    accepted = client.post("/cowork", json=body, headers=authorized_headers)
    assert accepted.status_code == 200
    assert accepted.json["actor"] == service.enrolled_actor().canonical_id
    assert accepted.json["actor"] != "attacker-selected"

    replay = client.post("/cowork", json=body, headers=authorized_headers)
    assert replay.status_code == 409
    assert replay.json["code"] == "gesture_replayed"

    _, remote_gesture = service.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action=action,
        subject=subject,
        context_sha256=exact_context,
    )
    remote = client.post(
        "/cowork",
        json=body,
        headers={**base_headers, "X-WB-Gesture": remote_gesture.token},
        environ_base={"REMOTE_ADDR": "100.64.0.8"},
    )
    assert remote.status_code == 403
    assert remote.json["code"] == "loopback_required"


def test_host_launch_places_one_time_grant_only_in_fragment(
    authority: tuple[LocalIdentityAuthority, Clock],
) -> None:
    service, _ = authority
    fragment = bootstrap_fragment_for_dashboard(
        "http://127.0.0.1:5127/app/",
        next_hash="#tab=journal",
        authority=service,
    )
    assert fragment.startswith("#wb-bootstrap=wbb_")
    assert "wb-next=%23tab%3Djournal" in fragment
    assert "?" not in fragment


def test_input_ingress_records_inputter_without_inventing_authorship(
    authority: tuple[LocalIdentityAuthority, Clock],
) -> None:
    service, _ = authority
    session = _session(service)
    action = "cowork.chat.message_send"
    subject = "cowork-conversation:conversation-security-test"
    context_sha256 = hashlib.sha256(b'exact request body').hexdigest()
    _, gesture = service.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action=action,
        subject=subject,
        context_sha256=context_sha256,
    )
    authorized = service.authorize_human_mutation(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        gesture_token=gesture.token,
        boundary=_boundary(),
        action=action,
        subject=subject,
        context_sha256=context_sha256,
    )

    ingress = authorized.to_input_ingress()
    assert ingress["inputter"] == service.enrolled_actor().to_dict()
    assert ingress["gesture_id"]
    assert len(ingress["session_id_sha256"]) == 64
    assert session.cookie_token not in ingress.values()
    assert not ({"author", "authorship", "reviewer", "reviewed"} & set(ingress))
