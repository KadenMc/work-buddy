import { describe, expect, it, vi } from "vitest";

import type { ChatExecutionSelection } from "../../../widget-library/chat";
import type { CoworkCapturedActionSnapshot } from "../targets";
import { HttpCoworkVerifyClient } from "./HttpCoworkVerifyClient";

const selection: ChatExecutionSelection = {
  providerId: "codex",
  modelId: "gpt-5.3-codex",
  providerLabel: "Codex",
  modelLabel: "GPT-5.3 Codex",
  revision: "rev-1",
};

const capture = {
  schema: "wb.cowork.action-snapshot/v1",
  captureId: "capture-1",
  storeId: "store-1",
  documentId: "doc-1",
  capturedAt: "2026-07-28T12:00:00.000Z",
  editGeneration: 3,
  ydocGenerationSha256: "a".repeat(64),
  snapshotBase64: "eWRvYw==",
  snapshotSha256: "b".repeat(64),
  stateVectorBase64: "c3RhdGU=",
  stateVectorSha256: "c".repeat(64),
  structuredHeadSha256: "d".repeat(64),
  projectionMarkdown: "# Title\n",
  projectionSha256: "e".repeat(64),
  projectionReceiptId: "projection-receipt-1",
  target: {
    source: "whole_document",
    label: "Whole document",
    wordCount: 1,
    proseMirrorRange: null,
    selector: { kind: "document" },
    targetTextSha256: "e".repeat(64),
  },
} satisfies CoworkCapturedActionSnapshot;

describe("HttpCoworkVerifyClient", () => {
  it("posts the exact capture and explicit displayed execution selection", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      new Response(
        JSON.stringify({
          action_snapshot_id: "action-1",
          run_id: "run-1",
          job_id: "job-1",
          stage: "reconciling",
          result_count: 0,
          coordination_status: "pending",
        }),
        { status: 202, headers: { "Content-Type": "application/json" } },
      ),
    );
    const client = new HttpCoworkVerifyClient({
      documentId: "doc-1",
      storeId: "store-1",
      fetchImpl: fetchImpl as typeof fetch,
    });

    await expect(
      client.startVerify(capture, selection, {
        userGoal: "Audit this PRD against the active criteria.",
        protectedIntent: "Preserve the user's product constraints.",
        recheckOfProposalIds: ["proposal-1"],
        recheckOfRunId: "source-run-1",
        recheckIntentId: "recheck-intent-1",
        recheckTargetConfirmation: {
          schema: "work-buddy.cowork-recheck-target-confirmation/v1",
          method: "user_affirmed_working_target",
          affirmedCaptureId: "affirmed-capture-1",
          affirmedActionSnapshotId: "affirmed-action-1",
          runCaptureId: "capture-1",
          targetReferenceSha256: "a".repeat(64),
          targetTextSha256: "b".repeat(64),
        },
      }),
    ).resolves.toMatchObject({
      runId: "run-1",
      coordinationStatus: "pending",
    });

    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("/api/truth/doc/doc-1/verify/runs?store_id=store-1");
    expect(init?.credentials).toBe("same-origin");
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    expect(body.capture).toEqual(capture);
    expect(body.execution).toEqual({
      provider_id: "codex",
      model_id: "gpt-5.3-codex",
      provider_label: "Codex",
      model_label: "GPT-5.3 Codex",
    });
    expect(body.recheck_of_proposal_ids).toEqual(["proposal-1"]);
    expect(body.recheck_of_run_id).toBe("source-run-1");
    expect(body.recheck_intent_id).toBe("recheck-intent-1");
    expect(body.recheck_target_confirmation).toEqual({
      schema: "work-buddy.cowork-recheck-target-confirmation/v1",
      method: "user_affirmed_working_target",
      affirmed_capture_id: "affirmed-capture-1",
      affirmed_action_snapshot_id: "affirmed-action-1",
      run_capture_id: "capture-1",
      target_reference_sha256: "a".repeat(64),
      target_text_sha256: "b".repeat(64),
    });
    expect(body.user_goal).toBe(
      "Audit this PRD against the active criteria.",
    );
    expect(body.protected_intent).toBe(
      "Preserve the user's product constraints.",
    );
    expect(body).not.toHaveProperty("expected_revision");
  });

  it("persists a non-executing target affirmation before Run", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      new Response(
        JSON.stringify({
          schema:
            "work-buddy.cowork-recheck-target-affirmation-receipt/v1",
          recheck_intent_id: "intent-1",
          source_run_id: "source-run-1",
          pending_proposal_ids: ["proposal-1"],
          affirmed_capture_id: capture.captureId,
          affirmed_action_snapshot_id: "affirmed-action-1",
          target_reference_sha256: "a".repeat(64),
          target_text_sha256: "b".repeat(64),
          affirmed_at: "2026-07-28T12:01:00.000Z",
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      ),
    );
    const client = new HttpCoworkVerifyClient({
      documentId: "doc-1",
      storeId: "store-1",
      fetchImpl: fetchImpl as typeof fetch,
    });

    await expect(
      client.affirmRecheckTarget(capture, {
        intentId: "intent-1",
        sourceRunId: "source-run-1",
        pendingProposalIds: ["proposal-1"],
        userGoal: "Check it.",
        protectedIntent: "Preserve intent.",
      }),
    ).resolves.toMatchObject({
      affirmedActionSnapshotId: "affirmed-action-1",
      recheckIntentId: "intent-1",
    });

    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe(
      "/api/truth/doc/doc-1/verify/recheck-target-affirmations?store_id=store-1",
    );
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    expect(body).toEqual({
      capture,
      recheck_intent_id: "intent-1",
      source_run_id: "source-run-1",
      proposal_ids: ["proposal-1"],
      user_goal: "Check it.",
      protected_intent: "Preserve intent.",
    });
  });

  it("keeps Co-think a separate explicit action and preserves safe errors", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            action_snapshot_id: "action-2",
            job_id: "job-2",
            status: "running",
          }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ error: "The exact version changed." }), {
          status: 409,
          headers: { "Content-Type": "application/json" },
        }),
      );
    const client = new HttpCoworkVerifyClient({
      documentId: "doc-1",
      storeId: "store-1",
      fetchImpl: fetchImpl as typeof fetch,
    });

    await expect(client.startCothink(capture, selection)).resolves.toMatchObject({
      jobId: "job-2",
    });
    expect(fetchImpl.mock.calls[0][0]).toBe(
      "/api/truth/doc/doc-1/cothink?store_id=store-1",
    );
    await expect(
      client.startVerify(capture, selection, {
        userGoal: "Check the active criteria.",
        protectedIntent: "Preserve the author's intent.",
      }),
    ).rejects.toThrow("The exact version changed.");
  });

  it("preserves a completed coordination receipt on idempotent replay", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      new Response(
        JSON.stringify({
          action_snapshot_id: "action-1",
          run_id: "run-1",
          job_id: "job-1",
          stage: "completed",
          result_count: 1,
          coordination_status: "completed",
        }),
        { status: 202, headers: { "Content-Type": "application/json" } },
      ),
    );
    const client = new HttpCoworkVerifyClient({
      documentId: "doc-1",
      storeId: "store-1",
      fetchImpl: fetchImpl as typeof fetch,
    });

    await expect(
      client.startVerify(capture, selection, {
        userGoal: "Check the active criteria.",
        protectedIntent: "Preserve the author's intent.",
      }),
    ).resolves.toMatchObject({ coordinationStatus: "completed" });
  });
});
