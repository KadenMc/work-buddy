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
