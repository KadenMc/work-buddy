import { describe, expect, it } from "vitest";

import type { R2DocPayload } from "./types";
import { mapR2ToReview } from "./reviewMapping";

const payload = (): R2DocPayload => ({
  document_id: "doc-1",
  store_id: "store-1",
  path: "brief.md",
  title: "Brief",
  profile: "co_authored",
  hashes: {
    ydoc_snapshot_sha256: "a".repeat(64),
    last_materialized_sha256: "b".repeat(64),
    current_file_sha256: "b".repeat(64),
  },
  drift: { state: "clean", diff_available: false },
  capabilities: {
    cowork_verify: {
      enabled: true,
      contract_version: 1,
      can_run: true,
      can_configure: false,
      can_cothink: true,
      disabled_reason: null,
    },
  },
  evaluation_run_summaries: [
    {
      run_id: "run-1",
      status: "completed",
      purpose: "document_review",
      target_label: "Whole document",
      coverage_label: "Complete exact-string scan",
      current_version: true,
      result_count: 1,
      surfaced_result_count: 1,
      coordination_status: "completed",
      provider_label: "Codex",
      model_label: "GPT",
      created_at: "2026-07-28T00:00:00Z",
      finished_at: "2026-07-28T00:00:01Z",
    },
  ],
  evaluation_results: [
    {
      result_id: "result-1",
      run_id: "run-1",
      kind: "nonconforming",
      criterion_label: "Approved terminology",
      criterion_statement: "Use document target.",
      check_label: "Terminology exact match",
      method_label: "Deterministic exact-string scan",
      explanation: "Deprecated terminology appears.",
      quote_anchor: { exact: "Co-work scope", prefix: "", suffix: "" },
      coverage_label: "Complete exact-string scan",
      limitations: [],
      current_version: true,
      disposition: "surface_result",
      canonical_sha256: "c".repeat(64),
      proposal_ids: [],
      created_at: "2026-07-28T00:00:01Z",
    },
  ],
  verification_recheck_intents: [
    {
      id: "recheck-1",
      sitting_id: "sitting-1",
      document_id: "doc-1",
      source_run_id: "run-1",
      proposal_ids: ["proposal-1"],
      pending_proposal_ids: ["proposal-1"],
      fulfilled_by_run_ids: [],
      committed_at: "2026-07-28T00:00:02Z",
      user_goal: "Recheck the applied terminology correction.",
      protected_intent: "Preserve substantive meaning.",
      status: "pending_capture",
      original_action_target: {
        action_snapshot_id: "action-1",
        source: "whole_document",
        label: "Whole document",
        kind: "document",
        selector: { kind: "document" },
        target_text_sha256: "d".repeat(64),
        target_reference: null,
        target_reference_sha256: null,
      },
      execution: {
        provider_id: "codex",
        model_id: "gpt",
        provider_label: "Codex",
        model_label: "GPT",
      },
      requires: {
        fresh_action_snapshot: true,
        fresh_model_call_authorization: true,
        same_target_source: true,
        same_target_reference: true,
        exact_target_resolution: true,
        user_affirmed_exact_target_required: false,
        on_unresolved: "user_action_required",
        allow_widen_to_whole_document: false,
      },
    },
  ],
  cothink_items: [],
  open_proposals: [],
  expressions: [],
  provenance_spans: [],
  events_cursor: "",
});

describe("Verify R2 mapping", () => {
  it("maps capability, run, and typed result without creating a proposal", () => {
    const mapped = mapR2ToReview(payload()).railData;
    expect(mapped.verifyCapability).toMatchObject({
      enabled: true,
      contractVersion: 1,
      canRun: true,
    });
    expect(mapped.evaluationRuns[0]?.runId).toBe("run-1");
    expect(mapped.evaluationResults[0]).toMatchObject({
      resultId: "result-1",
      kind: "nonconforming",
      disposition: "surface_result",
    });
    expect(mapped.verificationRecheckIntents[0]).toMatchObject({
      userGoal: "Recheck the applied terminology correction.",
      protectedIntent: "Preserve substantive meaning.",
      requires: {
        sameTargetSource: true,
        userAffirmedExactTargetRequired: false,
      },
    });
    expect(mapped.verificationRecheckIntents[0]).toMatchObject({
      intentId: "recheck-1",
      sourceRunId: "run-1",
      status: "pending_capture",
      execution: {
        providerId: "codex",
        modelId: "gpt",
      },
    });
    expect(mapped.proposals).toEqual([]);
  });

  it("carries the authoritative provider-aware execution disclosure", () => {
    const current: R2DocPayload = {
      ...payload(),
      verification_configuration: {
        schema: "work-buddy.cowork-verify-configuration/v1",
        document_id: "doc-1",
        execution_plan: {
          schema: "work-buddy.cowork-verify-execution-disclosure/v1",
          authoritative: true,
          checker: {
            execution_class: "in_process",
            mechanism: "deterministic_exact_match",
            model_call: false,
            external_egress: false,
            content_boundary: "captured_target",
          },
          coordination: {
            execution_class: "account_backed_agent",
            selection: {
              mode: "explicit_at_run_start",
              provider_id: null,
              model_id: null,
              provider_label: null,
              model_label: null,
            },
            content_boundary: "entire_frozen_document",
            external_egress: true,
            fallback: {
              provider_model_fallback: false,
              failure_mode: "fail_closed",
            },
            worker_sessions: {
              initial: 1,
              maximum: 3,
              conditional_roles: [
                "reviser",
                "post_revision_coordinator",
              ],
            },
            cost_control: null,
            provider_cost_controls: [
              {
                provider_id: "claude-code",
                enforcement_class: "hard_ceiling",
                ceiling_usd_per_worker_session: 2,
                basis: "claude_code_max_budget_usd",
              },
              {
                provider_id: "codex",
                enforcement_class: "unavailable",
                ceiling_usd_per_worker_session: null,
                basis: "codex_worker_has_no_budget_enforcement",
              },
            ],
          },
        },
        coordination: {
          deprecated: true,
          authoritative_projection: "execution_plan",
          required: true,
          selection: "explicit_provider_and_model_at_run_start",
          content_boundary: "entire_frozen_document",
          egress_class: "account_backed_agent",
          external_egress: true,
          cost_ceiling_usd_per_worker: 2,
          cost_ceiling_semantics:
            "requested_launch_budget_not_provider_guarantee",
          separate_reviser_for_findings: true,
          pattern:
            "coordinator_then_optional_reviser_then_coordinator",
          base_worker_calls: 1,
          maximum_worker_calls: 3,
        },
        criteria: [],
      },
    };

    const configuration =
      mapR2ToReview(current).railData.verificationConfiguration;

    expect(configuration.executionPlan).toMatchObject({
      authoritative: true,
      checker: {
        executionClass: "in_process",
        externalEgress: false,
      },
      coordination: {
        executionClass: "account_backed_agent",
        contentBoundary: "entire_frozen_document",
        externalEgress: true,
        fallback: {
          providerModelFallback: false,
          failureMode: "fail_closed",
        },
        workerSessions: { initial: 1, maximum: 3 },
      },
    });
    expect(
      configuration.executionPlan?.coordination.providerCostControls,
    ).toEqual([
      {
        providerId: "claude-code",
        enforcementClass: "hard_ceiling",
        ceilingUsdPerWorkerSession: 2,
        basis: "claude_code_max_budget_usd",
      },
      {
        providerId: "codex",
        enforcementClass: "unavailable",
        ceilingUsdPerWorkerSession: null,
        basis: "codex_worker_has_no_budget_enforcement",
      },
    ]);
    expect(configuration.coordination).toMatchObject({
      deprecated: true,
      authoritativeProjection: "execution_plan",
      costCeilingSemantics:
        "requested_launch_budget_not_provider_guarantee",
    });
  });

  it("fails closed when an older server omits capability negotiation", () => {
    const legacy = payload();
    delete (legacy as { capabilities?: unknown }).capabilities;
    expect(mapR2ToReview(legacy).railData.verifyCapability).toMatchObject({
      enabled: false,
      contractVersion: 0,
      canRun: false,
    });
  });
});
