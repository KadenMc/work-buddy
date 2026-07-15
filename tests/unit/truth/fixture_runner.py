"""Executable acceptance runner for the declarative truth workloads."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.export import ExportResult, export_store, import_store
from work_buddy.truth.identity import canonical_json, sha256_text
from work_buddy.truth.lifecycle import TruthLifecycle
from work_buddy.truth.locators import LocatorValidation, validate_locator
from work_buddy.truth.queries import (
    current_claims,
    integrity_findings,
    rebuild_claims_current,
    record_sweep,
    supersession_sweep_candidates,
)
from work_buddy.truth.redact import TruthRedactor, policy_basis_ref
from work_buddy.truth.store import AcquisitionOrigin, TruthStore


BASE_TIME = datetime(2026, 7, 14, 13, 0, tzinfo=timezone.utc)
HUMAN = Actor("human", "fixture-human")
SYSTEM = Actor("system", "fixture-system")
AGENT = Actor(
    "agent_run",
    "fixture-agent-run",
    {
        "model": "fixture-model",
        "harness": "pytest",
        "surface": "fixture-runner",
        "session_id": "fixture-session",
        "call_id": "fixture-call",
    },
)


@dataclass(frozen=True, slots=True)
class WorkloadResult:
    """Durable results from one complete workload and recovery round trip."""

    name: str
    store: TruthStore
    export_result: ExportResult
    restored_store: TruthStore
    runtime_ids: Mapping[str, str]


class EmptyRegistry:
    """Registry seam for an import target with no live identity collision."""

    def paths_for_store_id(self, store_id: str) -> tuple[Path, ...]:
        del store_id
        return ()


def load_workload(path: str | Path) -> dict[str, Any]:
    """Load one frozen declarative workload."""

    source = Path(path)
    value = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"workload must be a mapping: {source}")
    if value.get("fixture_version") != "wb-truth-fixture/v1":
        raise AssertionError(f"unsupported workload version: {source}")
    return value


def _timestamp(index: int) -> str:
    value = BASE_TIME + timedelta(minutes=index)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class WorkloadRunner:
    """Interpret fixture steps through the joined K0 public surface."""

    def __init__(self, store: TruthStore, fixture: Mapping[str, Any]) -> None:
        self.store = store
        self.fixture = dict(fixture)
        self.lifecycle = TruthLifecycle(store)
        self.redactor = TruthRedactor(store, lifecycle=self.lifecycle)
        self.ids = {
            str(alias): str(record_id)
            for alias, record_id in dict(self.fixture["ids"]).items()
        }
        self.runtime_ids = dict(self.ids)

    def ref(self, alias: str) -> str:
        try:
            return self.runtime_ids[alias]
        except KeyError as exc:
            raise AssertionError(f"unknown workload reference {alias!r}") from exc

    def run(self) -> WorkloadResult:
        for index, step_value in enumerate(self.fixture["steps"]):
            step = dict(step_value)
            operation = str(step["op"])
            handler = getattr(self, f"_run_{operation}", None)
            if handler is None:
                raise AssertionError(f"unsupported workload operation {operation!r}")
            handler(step, _timestamp(index))

        self._assert_expected_outcomes()
        rebuild_claims_current(self.store, rebuilt_at="2026-07-15T00:00:00Z")
        findings = integrity_findings(self.store)
        if self.fixture["name"] == "electricrag-sourced-successor-sweep":
            assert [item.code for item in findings] == [
                "confirmed_derivation_has_unconfirmed_premise"
            ]
            assert [item.severity for item in findings] == ["warning"]
            conn = self.store.connect()
            try:
                conclusion = conn.execute(
                    "SELECT claim_id FROM derivations WHERE id = ?",
                    (findings[0].subject_ref,),
                ).fetchone()[0]
            finally:
                conn.close()
            assert conclusion == self.ref("claim_threshold")
        else:
            assert findings == (), "workload left integrity findings: " + repr(findings)

        result = export_store(self.store)
        restored_root = self.store.paths.root.parent / (
            self.store.paths.root.name + "-restored"
        )
        restored_root.mkdir()
        imported = import_store(result.path, restored_root, registry=EmptyRegistry())
        restored_export = export_store(
            imported.store,
            restored_root / "restored-claims.jsonl",
        )
        assert restored_export.path.read_bytes() == result.path.read_bytes()
        return WorkloadResult(
            name=str(self.fixture["name"]),
            store=self.store,
            export_result=result,
            restored_store=imported.store,
            runtime_ids=dict(self.runtime_ids),
        )

    def _locator(
        self,
        input_data: Mapping[str, Any],
    ) -> tuple[str, LocatorValidation, str]:
        locator = str(input_data["locator"])
        content = str(input_data["content"])
        digest = sha256_text(content)
        if locator.lower().startswith("wb-session:"):
            kind = "chat"
        else:
            kind = "document"
        validation = validate_locator(
            kind,
            locator,
            input_data.get("locator_meta"),
            digest,
        )
        return kind, validation, digest

    def _run_capture(self, step: Mapping[str, Any], at: str) -> None:
        input_data = dict(step["input"])
        expected = dict(step["expect"])
        kind, validation, digest = self._locator(input_data)
        if kind == "chat":
            actor = SYSTEM
            method = "said_in_chat"
            origin = AcquisitionOrigin.MIXED_TRANSCRIPT
            reviewed = False
        elif validation.locator_scheme == "swh":
            actor = HUMAN
            method = "fetch"
            origin = AcquisitionOrigin.EXTERNAL
            reviewed = True
        else:
            actor = HUMAN
            method = "file_read"
            origin = AcquisitionOrigin.PREEXISTING
            reviewed = False

        meta = dict(validation.meta)
        meta["verifiability_class"] = validation.verifiability_class
        meta["integrity_recipe"] = dict(validation.integrity_recipe)
        evidence = self.store.capture_evidence(
            kind=kind,
            source_locator=validation.locator,
            actor=actor,
            acquisition_method=method,
            content=str(input_data["content"]),
            content_sha256=digest,
            media_type=str(input_data["media_type"]),
            acquired_at=at,
            created_at=at,
            origin=origin,
            external_reviewed=reviewed,
            meta=meta,
            record_id=self.ref(str(step["id"])),
        )
        assert evidence.content_sha256 == digest
        assert evidence.source_locator == validation.locator
        assert validation.verifiability_class in {"A", "B", "C", "D"}
        assert expected["source_state"] == "captured"
        if expected.get("digest_verified") and validation.locator_scheme == "swh":
            content = str(input_data["content"]).encode("utf-8")
            git_blob = b"blob " + str(len(content)).encode("ascii") + b"\0" + content
            actual_git_digest = hashlib.sha1(
                git_blob,
                usedforsecurity=False,
            ).hexdigest()
            expected_git_digest = str(validation.integrity_recipe["expected_digest"])
            assert actual_git_digest == expected_git_digest, (
                f"SWHID content digest mismatch: {actual_git_digest} != "
                f"{expected_git_digest}"
            )

    def _run_mark_span(self, step: Mapping[str, Any], at: str) -> None:
        input_data = dict(step["input"])
        selector_data = dict(input_data["selector"])
        evidence = self.store.get_evidence(self.ref(str(input_data["source_ref"])))
        assert evidence is not None
        span = self.store.mark_span(
            evidence_id=evidence.id,
            selector=CompositeSelector(
                exact=str(selector_data["exact"]),
                prefix=str(selector_data.get("prefix", "")),
                suffix=str(selector_data.get("suffix", "")),
            ),
            actor=HUMAN,
            author_kind="human" if evidence.trust_class == "mixed" else None,
            author_ref=HUMAN.ref if evidence.trust_class == "mixed" else None,
            record_id=self.ref(str(step["id"])),
            created_at=at,
        )
        assert span.quote_exact == selector_data["exact"]
        assert dict(step["expect"])["anchor_state"] == "exact"

    def _run_propose(self, step: Mapping[str, Any], at: str) -> None:
        input_data = dict(step["input"])
        created_at = str(input_data.get("proposed_at", at))
        meta: dict[str, Any] = {"basis": "suggested"}
        if input_data.get("policy_flags"):
            meta["policy_flags"] = list(input_data["policy_flags"])
        result = self.store.propose_claim(
            proposition=str(input_data["proposition"]),
            claim_kind=str(input_data["claim_kind"]),
            structured=input_data.get("structured"),
            actor=AGENT,
            valid_from=input_data.get("valid_from"),
            valid_to=input_data.get("valid_to"),
            meta=meta,
            record_id=self.ref(str(step["id"])),
            created_at=created_at,
            status_at=created_at,
        )
        assert result.created
        claim = result.claim
        evidence_span_ref = input_data.get("evidence_span_ref")
        if evidence_span_ref is not None:
            self.store.add_link(
                from_claim_id=claim.id,
                link_type="supports_span",
                to_kind="evidence_span",
                to_ref=self.ref(str(evidence_span_ref)),
                actor=AGENT,
                created_at=at,
            )
        premise_ref = input_data.get("premise_ref")
        if premise_ref is not None:
            self.store.add_derivation(
                claim_id=claim.id,
                method="fixture_dependency",
                premises=[self.ref(str(premise_ref))],
                actor=AGENT,
                created_at=at,
            )
        expected = dict(step["expect"])
        assert self.lifecycle.latest_status(claim.id).status == expected["status"]
        if "evidence_count" in expected:
            conn = self.store.connect()
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM claim_links WHERE from_claim_id = ? "
                    "AND link_type = 'supports_span'",
                    (claim.id,),
                ).fetchone()[0]
            finally:
                conn.close()
            assert count == expected["evidence_count"]
        if "expires_at" in expected:
            max_age = self.store.profile.proposal_max_age_seconds
            assert max_age is not None
            expires = _parse_timestamp(created_at) + timedelta(seconds=max_age)
            assert expires == _parse_timestamp(str(expected["expires_at"]))
        if "fold_state" in expected:
            assert expected["fold_state"] == "awaiting_micro_confirmation"
            assert "fold" in self.store.profile.gate.confirmation_surfaces
        if expected.get("materialization_blocked"):
            assert self.store.profile.gate.block_materialize_on_flags
            assert meta.get("policy_flags")

    def _run_derive(self, step: Mapping[str, Any], at: str) -> None:
        input_data = dict(step["input"])
        premises = [self.ref(str(alias)) for alias in input_data["premise_refs"]]
        derivation = self.store.add_derivation(
            claim_id=self.ref(str(input_data["conclusion_ref"])),
            method=str(input_data["method"]),
            premises=premises,
            actor=AGENT,
            record_id=self.ref(str(step["id"])),
            created_at=at,
        )
        expected = dict(step["expect"])
        assert expected["derivation_state"] == "valid"
        assert len(derivation.premises) == expected["premise_count"] == len(premises)

    def _run_confirm(self, step: Mapping[str, Any], at: str) -> None:
        input_data = dict(step["input"])
        claim = self.store.get_claim(self.ref(str(input_data["claim_ref"])))
        assert claim is not None
        gesture = self.lifecycle.mint_gesture(
            subject_ref=claim.id,
            actor=HUMAN,
            surface=str(input_data["surface"]),
            kind="confirm",
            displayed_payload_sha256=claim.canonical_sha256,
            at=at,
        )
        result = self.lifecycle.confirm_claim(
            claim_id=claim.id,
            gesture_id=gesture.id,
            actor=HUMAN,
            expected_context_sha256=None,
            observed_at=at,
            at=at,
        )
        assert result.event is not None
        expected = dict(step["expect"])
        assert result.event.status == expected["status"]
        assert result.gesture.consumed_at == at
        if "gesture_consumed" in expected:
            assert expected["gesture_consumed"]
        predecessor = expected.get("predecessor_status")
        if predecessor is not None:
            predecessor_id = self.ref("claim_eval_v1")
            assert self.lifecycle.latest_status(predecessor_id).status == predecessor

    def _run_supersede(self, step: Mapping[str, Any], at: str) -> None:
        input_data = dict(step["input"])
        link = self.lifecycle.supersede_claim(
            successor_claim_id=self.ref(str(input_data["successor_ref"])),
            predecessor_claim_id=self.ref(str(input_data["predecessor_ref"])),
            reason=str(input_data["supersession_reason"]),
            note=input_data.get("reason_note"),
            actor=AGENT,
            link_id=self.ref(str(step["id"])),
            created_at=at,
        )
        expected = dict(step["expect"])
        assert link.link_type == expected["link_kind"] == "supersedes"
        assert (
            self.lifecycle.latest_status(link.to_ref).status
            == expected["predecessor_status"]
        )
        assert (
            self.lifecycle.latest_status(link.from_claim_id).status
            == expected["successor_status"]
        )

    def _run_sweep(self, step: Mapping[str, Any], at: str) -> None:
        input_data = dict(step["input"])
        changed = self.ref(str(input_data["changed_claim_ref"]))
        dependent = self.ref(str(input_data["dependent_claim_ref"]))
        candidates = supersession_sweep_candidates(self.store, changed)
        candidate = next(item for item in candidates if item.subject_ref == dependent)
        recorded = record_sweep(
            self.store,
            kind="supersession",
            findings=candidates,
            params={"changed_claim_ref": changed},
            at=at,
            sweep_id=self.ref(str(step["id"])),
        )
        assert recorded.finding_ids
        finding_alias = str(dict(step["expect"])["finding_ref"])
        self.runtime_ids[finding_alias] = recorded.finding_ids[0]
        assert candidate.finding == f"depends_on_superseded_claim:{changed}"
        self.lifecycle.mark_needs_review(
            claim_id=dependent,
            actor=SYSTEM,
            basis_kind="sweep",
            basis_ref=recorded.sweep_id,
            note=candidate.finding,
            at=at,
        )
        expected = dict(step["expect"])
        assert expected["finding_kind"] == "superseded_premise"
        assert (
            self.lifecycle.latest_status(dependent).status
            == expected["dependent_status"]
        )

    def _run_expire(self, step: Mapping[str, Any], at: str) -> None:
        input_data = dict(step["input"])
        claim_id = self.ref(str(input_data["claim_ref"]))
        observed = str(input_data["observed_at"])
        transition = self.lifecycle.expire_claim(
            claim_id=claim_id,
            actor=SYSTEM,
            observed_at=observed,
            rule=str(input_data["basis"]),
            event_id=self.ref(str(step["id"])),
        )
        assert transition.event.status == "expired"
        reason = str(input_data["redaction_reason"])
        redaction = self.redactor.redact(
            subject_kind="claim",
            subject_ref=claim_id,
            actor=SYSTEM,
            reason=reason,
            basis_kind="policy",
            basis_ref=policy_basis_ref(self.store, reason),
            at=observed,
        )
        assert redaction.event.reason == reason
        claim = self.store.get_claim(claim_id)
        assert claim is not None
        assert claim.proposition == "[redacted]"
        assert claim.structured_json is None
        expected = dict(step["expect"])
        assert self.lifecycle.latest_status(claim_id).status == expected["status"]
        assert expected["proposition"] is None
        assert expected["structured"] is None
        assert not expected["content_retained"]
        assert redaction.event.reason == expected["redaction_reason"]

    def _run_redact(self, step: Mapping[str, Any], at: str) -> None:
        input_data = dict(step["input"])
        claim = self.store.get_claim(self.ref(str(input_data["claim_ref"])))
        assert claim is not None
        digest = claim.canonical_sha256
        gesture = self.lifecycle.mint_gesture(
            subject_ref=claim.id,
            actor=HUMAN,
            surface="dashboard",
            kind="redact",
            displayed_payload_sha256=digest,
            at=at,
        )
        result = self.redactor.redact(
            subject_kind="claim",
            subject_ref=claim.id,
            actor=HUMAN,
            reason=str(input_data["reason"]),
            basis_kind="gesture",
            basis_ref=gesture.id,
            event_id=self.ref(str(step["id"])),
            at=at,
        )
        redacted = self.store.get_claim(claim.id)
        assert redacted is not None
        assert redacted.canonical_sha256 == digest
        assert redacted.proposition == "[redacted]"
        assert result.status_event is not None
        expected = dict(step["expect"])
        assert result.status_event.status == expected["status"]
        assert expected["proposition"] is None
        assert expected["structured"] is None
        if "content_retained" in expected:
            assert not expected["content_retained"]
        assert expected["audit_metadata_retained"]

    def _run_materialize(self, step: Mapping[str, Any], at: str) -> None:
        input_data = dict(step["input"])
        manifest = json.loads(json.dumps(input_data["manifest"]))
        manifest["artifact_ref"] = self.ref(str(manifest["artifact_ref"]))
        for entry in manifest["entries"]:
            entry["claim_ref"] = self.ref(str(entry["claim_ref"]))
            if "derivation_ref" in entry:
                entry["derivation_ref"] = self.ref(str(entry["derivation_ref"]))
            assert (
                self.lifecycle.latest_status(entry["claim_ref"]).status == "confirmed"
            )

        primary = self.store.get_claim(manifest["entries"][0]["claim_ref"])
        assert primary is not None
        rendered = primary.proposition + "\n"
        target = (self.store.paths.root / str(input_data["path"])).resolve()
        root = self.store.paths.root.resolve()
        if root not in target.parents:
            raise InvariantViolation("materialized fixture path escaped its scope root")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
        with self.store.write_transaction() as conn:
            conn.execute(
                "INSERT INTO projections "
                "(id, path, rendered_at, content_sha256, manifest_json, health, "
                "health_reason) VALUES (?, ?, ?, ?, ?, 'clean', NULL)",
                (
                    self.ref(str(step["id"])),
                    str(input_data["path"]),
                    at,
                    sha256_text(rendered),
                    canonical_json(manifest),
                ),
            )
        assert target.read_text(encoding="utf-8") == rendered
        expected = dict(step["expect"])
        assert expected["artifact_state"] == "materialized"
        assert len(manifest["entries"]) == expected["manifest_claim_count"]
        assert expected["flagged_claim_count"] == 0

    def _assert_expected_outcomes(self) -> None:
        outcomes = dict(self.fixture["expected_outcomes"])
        name = str(self.fixture["name"])
        if name == "electricrag-sourced-successor-sweep":
            current = current_claims(self.store, claim_kind="measurement")
            assert [item.claim_id for item in current] == [
                self.ref(str(outcomes["current_measurement_ref"]))
            ]
            structured = json.loads(current[0].claim.structured_json or "{}")
            assert (
                structured["value"]["amount"] == outcomes["current_measurement_amount"]
            )
            assert (
                self.lifecycle.latest_status(self.ref("claim_eval_v1")).status
                == (outcomes["predecessor_status"])
            )
            assert (
                self.lifecycle.latest_status(self.ref("claim_threshold")).status
                == (outcomes["dependent_status_after_sweep"])
            )
            conn = self.store.connect()
            try:
                finding = conn.execute(
                    "SELECT finding FROM sweep_findings WHERE id = ?",
                    (self.ref(str(outcomes["sweep_finding_ref"])),),
                ).fetchone()
            finally:
                conn.close()
            assert finding is not None
            assert finding["finding"].startswith("depends_on_superseded_claim:")
            return
        if name == "my-career-confirmed-facts-artifact":
            for alias in outcomes["confirmed_fact_refs"]:
                assert self.lifecycle.latest_status(self.ref(str(alias))).status == (
                    "confirmed"
                )
            assert (
                self.lifecycle.latest_status(
                    self.ref(str(outcomes["confirmed_derived_ref"]))
                ).status
                == "confirmed"
            )
            conn = self.store.connect()
            try:
                row = conn.execute(
                    "SELECT manifest_json FROM projections WHERE id = ?",
                    (self.ref(str(outcomes["artifact_ref"])),),
                ).fetchone()
            finally:
                conn.close()
            assert row is not None
            manifest = json.loads(row["manifest_json"])
            assert [entry["claim_ref"] for entry in manifest["entries"]] == [
                self.ref(str(alias))
                for alias in outcomes["artifact_manifest_claim_refs"]
            ]
            return
        if name == "cothink-micro-confirmation-expiry-redaction":
            confirmed = self.lifecycle.latest_status(
                self.ref(str(outcomes["confirmed_claim_ref"]))
            )
            assert confirmed.status == "confirmed"
            conn = self.store.connect()
            try:
                surface = conn.execute(
                    "SELECT surface FROM gestures WHERE id = ?",
                    (confirmed.basis_ref,),
                ).fetchone()[0]
            finally:
                conn.close()
            assert surface == outcomes["confirmed_surface"]
            expired = self.store.get_claim(self.ref(str(outcomes["expired_claim_ref"])))
            redacted = self.store.get_claim(
                self.ref(str(outcomes["redacted_claim_ref"]))
            )
            assert expired is not None and expired.proposition == "[redacted]"
            expired_status = self.lifecycle.latest_status(expired.id)
            assert expired_status.status == "expired"
            assert str(expired_status.basis_ref).startswith(
                str(outcomes["expired_reason"])
            )
            assert not outcomes["expired_content_retained"]
            assert redacted is not None and redacted.proposition == "[redacted]"
            assert (
                self.lifecycle.latest_status(redacted.id).status
                == (outcomes["redacted_claim_status"])
            )
            assert not outcomes["redacted_content_retained"]
            return
        raise AssertionError(f"workload outcomes are not implemented for {name!r}")


__all__ = [
    "EmptyRegistry",
    "WorkloadResult",
    "WorkloadRunner",
    "load_workload",
]
