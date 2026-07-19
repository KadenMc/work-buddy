import type { Editor } from "@tiptap/core";
import { afterEach, describe, expect, it } from "vitest";

import { WbTrackedChangesAdapterImpl } from "../adapter";
import { readSuggestionAttrs } from "../attribution";
import type { AdapterEvents, DecisionItem } from "../types";
import { editProposal, makeSuggestionEditor, markSummary } from "./support";

let editor: Editor | undefined;

afterEach(() => {
  editor?.destroy();
  editor = undefined;
});

const attach = (content: string): WbTrackedChangesAdapterImpl => {
  editor = makeSuggestionEditor({ content });
  const adapter = new WbTrackedChangesAdapterImpl();
  adapter.attach(editor);
  return adapter;
};

const findMark = (ed: Editor, id: string, type: string) => {
  let found: ReturnType<typeof readSuggestionAttrs> | null = null;
  ed.state.doc.descendants((node) => {
    for (const mark of node.marks) {
      if (mark.type.name === type && String(mark.attrs["id"]) === id) {
        found = readSuggestionAttrs(mark);
      }
    }
    return true;
  });
  return found;
};

describe("WbTrackedChangesAdapter ingestion round-trip", () => {
  it("projects a quote-anchored edit as insertion and deletion marks carrying attribution", () => {
    const adapter = attach("<p>The quick brown fox</p>");
    const result = adapter.ingestProposal(
      editProposal("prop-1", "quick", "slow", { prefix: "The ", suffix: " brown" }),
    );

    expect(result.anchored).toBe(true);
    expect(adapter.listOpen()).toEqual(["prop-1"]);

    const marks = markSummary(editor as Editor);
    const deletion = marks.find((mark) => mark.type === "deletion");
    const insertion = marks.find((mark) => mark.type === "insertion");
    expect(deletion?.text).toBe("quick");
    expect(insertion?.text).toBe("slow");
    expect(deletion?.id).toBe("prop-1");
    expect(insertion?.id).toBe("prop-1");

    // Attribution attrs were stamped onto the tracked marks (SP-1 fork delta 2).
    const attrs = findMark(editor as Editor, "prop-1", "insertion");
    expect(attrs).toEqual({
      proposal_id: "prop-1",
      producer: "model-run-1",
      epistemic: "ai_proposed",
    });
  });

  it("reports an unresolvable anchor as not anchored and emits anchor:lost", () => {
    const adapter = attach("<p>The quick brown fox</p>");
    const lost: string[] = [];
    adapter.on("anchor:lost", (payload) => lost.push(payload.proposal_id));

    const result = adapter.ingestProposal(editProposal("prop-x", "absent phrase", "new"));
    expect(result.anchored).toBe(false);
    expect(lost).toEqual(["prop-x"]);
    expect(adapter.listOpen()).toEqual([]);
  });

  it("anchors a flag without minting any suggestion mark", () => {
    const adapter = attach("<p>The quick brown fox</p>");
    const result = adapter.ingestProposal({
      proposal_id: "flag-1",
      kind: "flag",
      quoteAnchor: { exact: "brown", prefix: "quick ", suffix: " fox" },
      replacement: null,
      attrs: { proposal_id: "flag-1", producer: "model-run-1", epistemic: "ai_proposed" },
      base_doc_sha256: "base",
      canonical_sha256: "canonical-flag-1",
    });
    expect(result.anchored).toBe(true);
    expect(markSummary(editor as Editor)).toEqual([]);
  });
});

describe("WbTrackedChangesAdapter accept and reject per id", () => {
  it("accepts one proposal, keeping the insertion text and dropping the marks", () => {
    const adapter = attach("<p>The quick brown fox</p>");
    adapter.ingestProposal(editProposal("prop-1", "quick", "slow", { prefix: "The " }));
    adapter.applyDecision({
      proposal_id: "prop-1",
      verb: "confirm",
      canonical_sha256: "canonical-prop-1",
    });

    expect(markSummary(editor as Editor)).toEqual([]);
    expect((editor as Editor).getText()).toContain("slow");
    expect((editor as Editor).getText()).not.toContain("quick");
    expect(adapter.listOpen()).toEqual([]);
  });

  it("rejects one proposal, restoring the original and dropping the insertion", () => {
    const adapter = attach("<p>The quick brown fox</p>");
    adapter.ingestProposal(editProposal("prop-1", "quick", "slow", { prefix: "The " }));
    adapter.applyDecision({
      proposal_id: "prop-1",
      verb: "reject_plain",
      canonical_sha256: "canonical-prop-1",
    });

    expect(markSummary(editor as Editor)).toEqual([]);
    expect((editor as Editor).getText()).toContain("quick");
    expect((editor as Editor).getText()).not.toContain("slow");
  });
});

describe("WbTrackedChangesAdapter overlap behavior", () => {
  it("accepts one of two adjacent proposals and leaves the other open", () => {
    const adapter = attach("<p>alpha beta gamma delta</p>");
    adapter.ingestProposal(editProposal("p-a", "alpha", "ALPHA", { suffix: " beta" }));
    adapter.ingestProposal(editProposal("p-b", "gamma", "GAMMA", { prefix: "beta ", suffix: " delta" }));
    expect(adapter.listOpen().sort()).toEqual(["p-a", "p-b"]);

    adapter.applyDecision({
      proposal_id: "p-a",
      verb: "confirm",
      canonical_sha256: "canonical-p-a",
    });

    expect(adapter.listOpen()).toEqual(["p-b"]);
    const text = (editor as Editor).getText();
    expect(text).toContain("ALPHA");
    // The second proposal's tracked pair still stands.
    const remaining = markSummary(editor as Editor).map((mark) => mark.id);
    expect(new Set(remaining)).toEqual(new Set(["p-b"]));
  });

  it("rejects one of two adjacent proposals independently", () => {
    const adapter = attach("<p>alpha beta gamma delta</p>");
    adapter.ingestProposal(editProposal("p-a", "alpha", "ALPHA", { suffix: " beta" }));
    adapter.ingestProposal(editProposal("p-b", "gamma", "GAMMA", { prefix: "beta ", suffix: " delta" }));

    adapter.applyDecision({
      proposal_id: "p-b",
      verb: "reject_plain",
      canonical_sha256: "canonical-p-b",
    });

    expect(adapter.listOpen()).toEqual(["p-a"]);
    const text = (editor as Editor).getText();
    expect(text).toContain("gamma");
    expect(text).not.toContain("GAMMA");
  });
});

describe("WbTrackedChangesAdapter staging and events", () => {
  it("stages and clears decisions without mutating the doc", () => {
    const adapter = attach("<p>The quick brown fox</p>");
    adapter.ingestProposal(editProposal("prop-1", "quick", "slow", { prefix: "The " }));

    const staged: DecisionItem[] = [];
    const cleared: string[] = [];
    adapter.on("decision:staged", (payload) => staged.push(payload.item));
    adapter.on("decision:cleared", (payload) => cleared.push(payload.proposal_id));

    const item: DecisionItem = {
      proposal_id: "prop-1",
      verb: "confirm",
      canonical_sha256: "canonical-prop-1",
    };
    adapter.stageDecision(item);
    expect(adapter.collectSitting()).toEqual([item]);
    // Staging never commits: the marks are still present.
    expect(adapter.listOpen()).toEqual(["prop-1"]);

    adapter.clearDecision("prop-1");
    expect(adapter.collectSitting()).toEqual([]);
    expect(staged).toEqual([item]);
    expect(cleared).toEqual(["prop-1"]);
  });

  it("emits proposals:changed with the open set on ingest", () => {
    const adapter = attach("<p>alpha beta gamma</p>");
    const opens: string[][] = [];
    adapter.on("proposals:changed", (payload: AdapterEvents["proposals:changed"]) =>
      opens.push(payload.open),
    );
    adapter.ingestProposal(editProposal("p-a", "alpha", "ALPHA", { suffix: " beta" }));
    adapter.ingestProposal(editProposal("p-b", "gamma", "GAMMA", { prefix: "beta " }));

    const last = opens[opens.length - 1];
    expect([...last].sort()).toEqual(["p-a", "p-b"]);
  });

  it("re-anchors a proposal by quote and emits anchor:reanchored", () => {
    const adapter = attach("<p>The quick brown fox</p>");
    adapter.ingestProposal(editProposal("prop-1", "brown", "red", { prefix: "quick ", suffix: " fox" }));

    const events: AdapterEvents["anchor:reanchored"][] = [];
    adapter.on("anchor:reanchored", (payload) => events.push(payload));
    const range = adapter.reanchor("prop-1");
    expect(range).not.toBeNull();
    expect(events).toHaveLength(1);
    expect(events[0].proposal_id).toBe("prop-1");
  });
});
