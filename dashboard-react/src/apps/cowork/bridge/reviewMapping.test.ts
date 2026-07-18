import { describe, expect, it } from "vitest";

import {
  deriveAnchorLabel,
  deriveChangeType,
  mapR2ToReview,
} from "./reviewMapping";
import type { R2DocPayload, R2Proposal } from "./types";

const producer = {
  model: "research-agent",
  model_source: "session-manifest",
  session_id: "sess-1",
  surface: "mcp",
} as const;

const proposal = (over: Partial<R2Proposal>): R2Proposal => ({
  proposal_id: "p1",
  kind: "edit",
  quote_anchor: { exact: "the cache key", prefix: "keys on ", suffix: "," },
  replacement: "the cache key and the vault hash",
  rationale: "Keys must include vault state.",
  tldr: "Add the vault hash.",
  producer,
  epistemic_state: "ai_proposed",
  base_doc_sha256: "base",
  canonical_sha256: "canon-p1",
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
    ydoc_snapshot_sha256: "ysnap",
    last_materialized_sha256: "matsha",
    current_file_sha256: "filesha",
  },
  drift: { state: "clean", diff_available: false },
  open_proposals: proposals,
  expressions: [],
  provenance_spans: [],
  events_cursor: "cursor-0",
});

describe("deriveChangeType", () => {
  it("classifies a cleared replacement as a deletion", () => {
    expect(deriveChangeType(proposal({ replacement: "" }))).toBe("deletion");
  });

  it("classifies a replacement that keeps the quote as an insertion", () => {
    expect(
      deriveChangeType(
        proposal({
          quote_anchor: { exact: "cache", prefix: "", suffix: "" },
          replacement: "cache and vault hash",
        }),
      ),
    ).toBe("insertion");
  });

  it("classifies a rewrite as a modification", () => {
    expect(
      deriveChangeType(
        proposal({
          quote_anchor: { exact: "always", prefix: "", suffix: "" },
          replacement: "sometimes",
        }),
      ),
    ).toBe("modification");
  });

  it("gives a flag no change type", () => {
    expect(
      deriveChangeType(proposal({ kind: "flag", replacement: null })),
    ).toBeUndefined();
  });
});

describe("deriveAnchorLabel", () => {
  it("quotes a short exact snippet", () => {
    expect(deriveAnchorLabel(proposal({ quote_anchor: { exact: "hello", prefix: "", suffix: "" } }))).toBe(
      '"hello"',
    );
  });

  it("truncates a long snippet with an ellipsis", () => {
    const long = "a".repeat(80);
    const label = deriveAnchorLabel(
      proposal({ quote_anchor: { exact: long, prefix: "", suffix: "" } }),
    );
    expect(label.startsWith('"')).toBe(true);
    expect(label.endsWith('…"')).toBe(true);
    expect(label.length).toBeLessThan(long.length);
  });
});

describe("mapR2ToReview", () => {
  it("projects one payload into rail cards and ingestion inputs from one array", () => {
    const mapped = mapR2ToReview(
      payload([
        proposal({ proposal_id: "s1" }),
        proposal({ proposal_id: "f1", kind: "flag", replacement: null }),
      ]),
    );

    // Both projections are derived from the same open_proposals array, so the ids agree.
    expect(mapped.railData.proposals.map((p) => p.proposalId)).toEqual(["s1", "f1"]);
    expect(mapped.proposalInputs.map((p) => p.proposal_id)).toEqual(["s1", "f1"]);
  });

  it("carries the field-name aliases through to the rail shape", () => {
    const mapped = mapR2ToReview(payload([proposal({ proposal_id: "s1" })]));
    const card = mapped.railData.proposals[0];
    expect(card.baseDocSha256).toBe("base");
    expect(card.canonicalSha256).toBe("canon-p1");
    expect(card.producer.modelSource).toBe("session-manifest");
    expect(card.documentOrder).toBe(0);
  });

  it("computes the drift health from the open set and the hashes", () => {
    const mapped = mapR2ToReview(
      payload([
        proposal({ proposal_id: "s1" }),
        proposal({ proposal_id: "f1", kind: "flag", replacement: null }),
      ]),
    );
    expect(mapped.railData.drift).toEqual({
      state: "clean",
      openProposalCount: 2,
      openFlagCount: 1,
      lastMaterializedSha256: "matsha",
      currentFileSha256: "filesha",
    });
  });

  it("defaults a missing claim-ref role to instantiation", () => {
    const mapped = mapR2ToReview(
      payload([
        proposal({
          proposal_id: "s1",
          claim_refs: [{ claim: "wb-truth://demo/claim/c1" }],
        }),
      ]),
    );
    expect(mapped.railData.proposals[0].claimRefs).toEqual([
      { claim: "wb-truth://demo/claim/c1", role: "instantiation" },
    ]);
  });

  it("maps the ingestion input attribution from the producer session", () => {
    const mapped = mapR2ToReview(payload([proposal({ proposal_id: "s1" })]));
    expect(mapped.proposalInputs[0].attrs).toEqual({
      proposal_id: "s1",
      producer: "sess-1",
      epistemic: "ai_proposed",
    });
  });
});
