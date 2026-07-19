import { afterEach, describe, expect, it } from "vitest";
import type { Editor } from "@tiptap/core";

import { createWbTrackedChangesAdapter } from "../suggestions/adapter";
import { makeSuggestionEditor } from "../suggestions/__tests__/support";
import { mapR2ToReview } from "./reviewMapping";
import { ProposalIngestor } from "./proposalIngestor";
import type { R2DocPayload, R2Proposal } from "./types";

const producer = {
  model: "research-agent",
  model_source: "session-manifest",
  session_id: "sess-1",
  surface: "mcp",
} as const;

const proposal = (over: Partial<R2Proposal>): R2Proposal => ({
  proposal_id: "p",
  kind: "edit",
  quote_anchor: { exact: "", prefix: "", suffix: "" },
  replacement: "",
  rationale: "r",
  tldr: "t",
  producer,
  epistemic_state: "ai_proposed",
  base_doc_sha256: "base",
  canonical_sha256: "canon",
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
    last_materialized_sha256: null,
    current_file_sha256: "filesha",
  },
  drift: { state: "clean", diff_available: false },
  open_proposals: proposals,
  expressions: [],
  provenance_spans: [],
  events_cursor: "c0",
});

const SCENE = payload([
  proposal({
    proposal_id: "s1",
    quote_anchor: { exact: "the cache key", prefix: "hash ", suffix: " for" },
    replacement: "the cache key and the vault hash",
  }),
  proposal({
    proposal_id: "s2",
    quote_anchor: { exact: "reuse", prefix: "for ", suffix: "." },
    replacement: "",
  }),
  proposal({
    proposal_id: "f1",
    kind: "flag",
    quote_anchor: { exact: "The benchmark figure", prefix: "", suffix: " needs" },
    replacement: null,
  }),
]);

let editor: Editor | null = null;

afterEach(() => {
  editor?.destroy();
  editor = null;
});

describe("ProposalIngestor", () => {
  it("projects the open edit proposals so the marks agree with the cards", () => {
    editor = makeSuggestionEditor({
      content:
        "<p>Keys hash the cache key for reuse.</p><p>The benchmark figure needs a citation.</p>",
    });
    const adapter = createWbTrackedChangesAdapter();
    adapter.attach(editor);

    const mapped = mapR2ToReview(SCENE);
    const ingestor = new ProposalIngestor();
    ingestor.attach(adapter);
    ingestor.setProposals(mapped.proposalInputs);

    // Cards equal marks: the rail's edit-proposal ids equal the adapter's open mark ids.
    const cardEditIds = mapped.railData.proposals
      .filter((card) => card.kind === "edit")
      .map((card) => card.proposalId)
      .sort();
    expect([...adapter.listOpen()].sort()).toEqual(cardEditIds);
    expect(cardEditIds).toEqual(["s1", "s2"]);

    // A flag anchors a span without a mark, so it is projected but never in listOpen.
    expect([...ingestor.anchoredIds()].sort()).toEqual(["f1", "s1", "s2"]);
  });

  it("ingests before the pull when the editor mounts late", () => {
    editor = makeSuggestionEditor({
      content: "<p>Keys hash the cache key for reuse.</p>",
    });
    const adapter = createWbTrackedChangesAdapter();
    adapter.attach(editor);

    const ingestor = new ProposalIngestor();
    // The pull arrives before the adapter is attached (editor mounting later).
    ingestor.setProposals(
      mapR2ToReview(payload([
        proposal({
          proposal_id: "s1",
          quote_anchor: { exact: "the cache key", prefix: "hash ", suffix: " for" },
          replacement: "the cache key and the vault hash",
        }),
      ])).proposalInputs,
    );
    expect(adapter.listOpen()).toEqual([]);

    ingestor.attach(adapter);
    expect(adapter.listOpen()).toEqual(["s1"]);
  });

  it("does not re-ingest an already projected proposal on a reload", () => {
    editor = makeSuggestionEditor({
      content: "<p>Keys hash the cache key for reuse.</p>",
    });
    const adapter = createWbTrackedChangesAdapter();
    adapter.attach(editor);

    const inputs = mapR2ToReview(payload([
      proposal({
        proposal_id: "s1",
        quote_anchor: { exact: "the cache key", prefix: "hash ", suffix: " for" },
        replacement: "the cache key and the vault hash",
      }),
    ])).proposalInputs;

    const ingestor = new ProposalIngestor();
    ingestor.attach(adapter);
    ingestor.setProposals(inputs);
    ingestor.setProposals(inputs);

    expect(adapter.listOpen()).toEqual(["s1"]);
  });
});
