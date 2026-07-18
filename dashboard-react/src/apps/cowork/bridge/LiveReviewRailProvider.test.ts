import { describe, expect, it, vi } from "vitest";

import { InMemoryCoworkSittingTransport } from "../suggestions/sitting";
import type { DecisionItem } from "../suggestions/types";
import type { SittingSubmission } from "../rail/provider";
import type { DecisionApplier } from "./sittingSubmit";
import { LiveReviewRailProvider } from "./LiveReviewRailProvider";
import type { CoworkDocClient } from "./HttpCoworkDocClient";
import type { R2DocPayload, R2Proposal } from "./types";

const producer = {
  model: "research-agent",
  model_source: "session-manifest",
  session_id: "sess-1",
  surface: "mcp",
} as const;

const proposal = (over: Partial<R2Proposal>): R2Proposal => ({
  proposal_id: "s1",
  kind: "edit",
  quote_anchor: { exact: "the cache key", prefix: "", suffix: "" },
  replacement: "the cache key and vault hash",
  rationale: "r",
  tldr: "t",
  producer,
  epistemic_state: "ai_proposed",
  base_doc_sha256: "base",
  canonical_sha256: "canon-s1",
  base_ok: true,
  status: "open",
  fixes_ref: null,
  claim_refs: [],
  created_at: "2026-07-17T12:00:00Z",
  ...over,
});

const payload = (proposals: readonly R2Proposal[]): R2DocPayload => ({
  document_id: "doc-1",
  store_id: "store-1",
  path: "docs/demo.md",
  title: "demo.md",
  profile: "co_authored",
  hashes: {
    ydoc_snapshot_sha256: null,
    last_materialized_sha256: "matsha",
    current_file_sha256: "filesha",
  },
  drift: { state: "clean", diff_available: false },
  open_proposals: proposals,
  expressions: [],
  provenance_spans: [],
  events_cursor: "c0",
});

const docClientReturning = (value: R2DocPayload): CoworkDocClient => ({
  fetchDoc: async () => value,
});

const applierRecording = () => {
  const applied: DecisionItem[] = [];
  const applier: DecisionApplier = { applyDecision: (item) => applied.push(item) };
  return { applier, applied };
};

const build = (options?: {
  readonly doc?: R2DocPayload;
  readonly getAdapter?: () => DecisionApplier | null;
}) =>
  new LiveReviewRailProvider({
    docClient: docClientReturning(options?.doc ?? payload([proposal({})])),
    documentId: "doc-1",
    storeId: "store-1",
    sittingTransport: new InMemoryCoworkSittingTransport(),
    getAdapter: options?.getAdapter ?? (() => ({ applyDecision: () => {} })),
    renderMaterialized: async () => "# materialized\n",
  });

describe("LiveReviewRailProvider", () => {
  it("load returns the rail data from the R2 pull", async () => {
    const provider = build();
    const data = await provider.load();
    expect(data.title).toBe("demo.md");
    expect(data.proposals.map((p) => p.proposalId)).toEqual(["s1"]);
    expect(data.drift.currentFileSha256).toBe("filesha");
  });

  it("emits the same pull to the ingestion and health channels", async () => {
    const provider = build({
      doc: payload([proposal({ proposal_id: "s1" }), proposal({ proposal_id: "s2" })]),
    });
    const proposals = vi.fn();
    const data = vi.fn();
    provider.onProposals(proposals);
    provider.onData(data);

    await provider.load();

    expect(proposals).toHaveBeenCalledTimes(1);
    expect(proposals.mock.calls[0][0].map((p: { proposal_id: string }) => p.proposal_id)).toEqual([
      "s1",
      "s2",
    ]);
    expect(data).toHaveBeenCalledTimes(1);
    expect(data.mock.calls[0][0].proposals.map((p: { proposalId: string }) => p.proposalId)).toEqual([
      "s1",
      "s2",
    ]);
  });

  it("replays the last pull to a late subscriber", async () => {
    const provider = build();
    await provider.load();
    const late = vi.fn();
    provider.onProposals(late);
    expect(late).toHaveBeenCalledTimes(1);
  });

  it("fans an invalidation out to the rail's reload listeners", async () => {
    const provider = build();
    const reload = vi.fn();
    provider.subscribe(reload);
    provider.invalidate();
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("submitSitting delegates to the sitting path through the adapter", async () => {
    const { applier, applied } = applierRecording();
    const provider = build({ getAdapter: () => applier });
    const submission: SittingSubmission = {
      baseDocSha256: "base-sha",
      proposalDecisions: [
        { proposalId: "s1", verb: "confirm", canonicalSha256: "canon-s1" },
      ],
      claimDecisions: [],
    };

    const result = await provider.submitSitting(submission);

    expect(applied.map((item) => item.proposal_id)).toEqual(["s1"]);
    expect(result.results[0]?.result).toBe("applied");
  });

  it("throws when the editor adapter is not ready", async () => {
    const provider = build({ getAdapter: () => null });
    await expect(
      provider.submitSitting({
        baseDocSha256: "base-sha",
        proposalDecisions: [
          { proposalId: "s1", verb: "confirm", canonicalSha256: "canon-s1" },
        ],
        claimDecisions: [],
      }),
    ).rejects.toThrow(/adapter is not ready/u);
  });
});
