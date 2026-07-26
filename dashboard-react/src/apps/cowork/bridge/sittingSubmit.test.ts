import { describe, expect, it, vi } from "vitest";

import type { SittingSubmission } from "../rail/provider";
import type { CoworkSittingTransport } from "../suggestions/sitting";
import type {
  DecisionItem,
  SittingItemResult,
  SittingPrepared,
  SittingResponse,
} from "../suggestions/types";
import { submitCoworkSitting, toDecisionItem } from "./sittingSubmit";
import type { CoworkSittingWorkspace } from "./sittingWorkspace";

const staged = (proposalId: string, verb: DecisionItem["verb"] = "confirm") => ({
  proposalId,
  verb,
  canonicalSha256: "c".repeat(64),
});

const submission = (items = [staged("p1")]): SittingSubmission => ({
  baseDocSha256: "f".repeat(64),
  proposalDecisions: items,
  claimDecisions: [],
});

const itemResult = (
  item: DecisionItem,
  result: SittingItemResult["result"] = "applied",
): SittingItemResult => ({
  proposal_id: item.proposal_id,
  verb: item.verb,
  result,
  base_ok: result !== "rejected_stale_view",
  gesture_id: result === "rejected_stale_view" ? null : `g-${item.proposal_id}`,
  negation_claim_id: null,
  preference_claim_id: null,
  new_proposal_id: null,
  materialized: result === "applied",
  error: result === "rejected_stale_view" ? "stale" : null,
});

const receipt = (intentId: string, results: readonly SittingItemResult[]): SittingResponse => ({
  ok: true,
  intent_id: intentId,
  partial: results.some((item) => item.result === "rejected_stale_view"),
  results,
  materialize: { new_file_sha256: "m".repeat(64), document_version_id: "v1" },
  structured_head_sha256: "s".repeat(64),
  snapshot_sha256: "s".repeat(64),
});

const prepared = (
  items: readonly DecisionItem[],
  failed: readonly SittingItemResult[] = [],
): SittingPrepared => ({
  ok: true,
  intent_id: "intent-1",
  state: "prepared",
  expires_at: "later",
  expected_file_sha256: "f".repeat(64),
  expected_ydoc_head_sha256: "h".repeat(64),
  expected_snapshot_sha256: "b".repeat(64),
  admitted_items: items,
  failed_items: failed,
  requires_document_commit: items.some(
    (item) => item.verb === "confirm" || item.verb === "edit_confirm",
  ),
});

const workspace = (events: string[]): CoworkSittingWorkspace => ({
  synchronize: async () => ({
    expectedFileSha256: "f".repeat(64),
    expectedStructuredHeadSha256: "h".repeat(64),
    generation: 4,
  }),
  prepare: async (items, generation) => {
    events.push(`prepared:${items.map((item) => item.proposal_id).join(",")}`);
    return {
      generation,
      commit: {
        snapshot: new Uint8Array([1]),
        snapshot_sha256: "s".repeat(64),
        rendered_markdown: "# committed\n",
        rendered_sha256: "m".repeat(64),
      },
      adopt: () => events.push("adopted"),
      dispose: () => events.push("disposed"),
    };
  },
  isCurrent: () => true,
  refreshFromServer: async () => {
    events.push("refreshed");
  },
});

describe("submitCoworkSitting two-phase choreography", () => {
  it("keeps live state untouched until commit and applies only server-admitted items", async () => {
    const decisions = [toDecisionItem(staged("accepted")), toDecisionItem(staged("stale"))];
    const events: string[] = [];
    const transport: CoworkSittingTransport = {
      prepare: async () => prepared([decisions[0]], [itemResult(decisions[1], "rejected_stale_view")]),
      commit: async (request) => {
        expect(events).toEqual(["prepared:accepted"]);
        expect(request.documentCommit?.rendered_markdown).toBe("# committed\n");
        events.push("server-committed");
        return receipt("intent-1", [
          itemResult(decisions[0]),
          itemResult(decisions[1], "rejected_stale_view"),
        ]);
      },
      cancel: async () => undefined,
    };

    const result = await submitCoworkSitting({
      documentId: "doc",
      storeId: "store",
      submission: submission([staged("accepted"), staged("stale")]),
      workspace: workspace(events),
      transport,
      idempotencyKeyFor: () => "stable-key",
    });

    expect(events).toEqual([
      "prepared:accepted",
      "server-committed",
      "adopted",
      "disposed",
    ]);
    expect(result.partial).toBe(true);
    expect(result.results[1]?.result).toBe("rejected_stale_view");
  });

  it("does not adopt on commit failure and preserves a stable retry key", async () => {
    const events: string[] = [];
    const keys: string[] = [];
    const item = toDecisionItem(staged("p1"));
    const transport: CoworkSittingTransport = {
      prepare: async (request) => {
        keys.push(request.body.idempotency_key);
        return prepared([item]);
      },
      commit: async () => {
        throw new TypeError("network lost after request");
      },
      cancel: async () => undefined,
    };
    const stableKey = vi.fn(() => "stable-key");
    await expect(
      submitCoworkSitting({
        documentId: "doc",
        storeId: "store",
        submission: submission(),
        workspace: workspace(events),
        transport,
        idempotencyKeyFor: stableKey,
      }),
    ).rejects.toThrow(/network/u);
    expect(events).toEqual(["prepared:p1", "disposed"]);
    expect(keys).toEqual(["stable-key"]);
  });

  it("recovers a response-lost committed intent by refreshing instead of reapplying", async () => {
    const events: string[] = [];
    const item = toDecisionItem(staged("p1"));
    const committed = receipt("intent-1", [itemResult(item)]);
    const transport: CoworkSittingTransport = {
      prepare: async () => ({ ...prepared([item]), state: "committed", result: committed }),
      commit: vi.fn(),
      cancel: async () => undefined,
    };
    await submitCoworkSitting({
      documentId: "doc",
      storeId: "store",
      submission: submission(),
      workspace: workspace(events),
      transport,
      idempotencyKeyFor: () => "stable-key",
    });
    expect(events).toEqual(["refreshed"]);
    expect(transport.commit).not.toHaveBeenCalled();
  });

  it("defensively rejects mixed claim decisions before synchronization or transport", async () => {
    const synchronize = vi.fn();
    const transport: CoworkSittingTransport = {
      prepare: vi.fn(),
      commit: vi.fn(),
      cancel: vi.fn(),
    };
    await expect(
      submitCoworkSitting({
        documentId: "doc",
        storeId: "store",
        submission: {
          ...submission(),
          claimDecisions: [
            { claimId: "claim-1", verb: "confirm", canonicalSha256: "claim-sha" },
          ],
        },
        workspace: {
          ...workspace([]),
          synchronize,
        },
        transport,
        idempotencyKeyFor: vi.fn(),
      }),
    ).rejects.toThrow(/No sitting decisions were submitted/u);
    expect(synchronize).not.toHaveBeenCalled();
    expect(transport.prepare).not.toHaveBeenCalled();
    expect(transport.commit).not.toHaveBeenCalled();
  });
});
