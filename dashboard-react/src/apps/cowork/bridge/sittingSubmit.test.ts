import { afterEach, describe, expect, it } from "vitest";
import type { Editor } from "@tiptap/core";

import { createWbTrackedChangesAdapter } from "../suggestions/adapter";
import { InMemoryCoworkSittingTransport } from "../suggestions/sitting";
import type { DecisionItem } from "../suggestions/types";
import { editProposal, makeSuggestionEditor } from "../suggestions/__tests__/support";
import type { SittingSubmission } from "../rail/provider";
import type { StagedDecision } from "../rail/contracts";
import {
  submitCoworkSitting,
  toDecisionItem,
  type DecisionApplier,
} from "./sittingSubmit";

const staged = (over: Partial<StagedDecision> & Pick<StagedDecision, "proposalId" | "verb">): StagedDecision => ({
  canonicalSha256: `canon-${over.proposalId}`,
  ...over,
});

const submission = (
  proposalDecisions: readonly StagedDecision[],
): SittingSubmission => ({
  baseDocSha256: "base-sha",
  proposalDecisions,
  claimDecisions: [],
});

/** An applier double that records the decisions the submit path applied to the editor. */
const recordingApplier = () => {
  const applied: DecisionItem[] = [];
  const applier: DecisionApplier = {
    applyDecision: (item) => applied.push(item),
  };
  return { applier, applied };
};

let editor: Editor | null = null;
afterEach(() => {
  editor?.destroy();
  editor = null;
});

describe("toDecisionItem", () => {
  it("translates the rail camelCase fields to the R5 wire item", () => {
    expect(
      toDecisionItem(
        staged({
          proposalId: "s1",
          verb: "edit_confirm",
          amendContent: "amended text",
        }),
      ),
    ).toEqual({
      proposal_id: "s1",
      verb: "edit_confirm",
      canonical_sha256: "canon-s1",
      amend_content: "amended text",
    });
  });

  it("carries the reject_as_preference verbatim phrasing (FA-1)", () => {
    expect(
      toDecisionItem(
        staged({
          proposalId: "s2",
          verb: "reject_as_preference",
          preferenceText: "my preferred wording",
        }),
      ),
    ).toEqual({
      proposal_id: "s2",
      verb: "reject_as_preference",
      canonical_sha256: "canon-s2",
      preference_text: "my preferred wording",
    });
  });
});

describe("submitCoworkSitting", () => {
  it("applies each decision, posts the items, and carries a materialize block for accepts", async () => {
    const { applier, applied } = recordingApplier();
    const transport = new InMemoryCoworkSittingTransport();

    const result = await submitCoworkSitting({
      documentId: "doc-1",
      storeId: "store-1",
      submission: submission([
        staged({ proposalId: "s1", verb: "confirm" }),
        staged({ proposalId: "s2", verb: "reject_plain" }),
      ]),
      adapter: applier,
      transport,
      renderMaterialized: async () => "# materialized\n",
    });

    // Every staged decision was applied to the editor before the post.
    expect(applied.map((item) => item.proposal_id)).toEqual(["s1", "s2"]);

    const request = transport.lastRequest;
    expect(request?.documentId).toBe("doc-1");
    expect(request?.storeId).toBe("store-1");
    expect(request?.body.base_doc_sha256).toBe("base-sha");
    expect(request?.body.items.map((item) => item.verb)).toEqual([
      "confirm",
      "reject_plain",
    ]);
    // A sitting with an accept carries the materialize block (section 1.5).
    expect(request?.body.materialize?.rendered_markdown).toBe("# materialized\n");

    // The response maps back to the rail shape.
    expect(result.ok).toBe(true);
    const byId = new Map(result.results.map((item) => [item.proposalId, item]));
    expect(byId.get("s1")?.result).toBe("applied");
    expect(byId.get("s2")?.result).toBe("closed");
  });

  it("omits the materialize block and never renders when the sitting has no accept", async () => {
    const { applier } = recordingApplier();
    const transport = new InMemoryCoworkSittingTransport();

    await submitCoworkSitting({
      documentId: "doc-1",
      storeId: "store-1",
      submission: submission([
        staged({ proposalId: "s1", verb: "redirect", redirectNote: "reconsider the scope" }),
      ]),
      adapter: applier,
      transport,
      renderMaterialized: async () => {
        throw new Error("must not render markdown without an accept");
      },
    });

    expect(transport.lastRequest?.body.materialize).toBeNull();
  });

  it("accept round trip: applies the suggestion to the editor and clears the mark", async () => {
    editor = makeSuggestionEditor({
      content: "<p>Keys hash the cache key for reuse.</p>",
    });
    const adapter = createWbTrackedChangesAdapter();
    adapter.attach(editor);
    adapter.ingestProposal(
      editProposal("s1", "the cache key", "the cache key and the vault hash", {
        prefix: "hash ",
        suffix: " for",
      }),
    );
    expect(adapter.listOpen()).toEqual(["s1"]);

    const transport = new InMemoryCoworkSittingTransport();
    await submitCoworkSitting({
      documentId: "doc-1",
      storeId: "store-1",
      submission: submission([staged({ proposalId: "s1", verb: "confirm" })]),
      adapter,
      transport,
      renderMaterialized: async () => editor?.getText() ?? "",
    });

    // The accepted edit is applied and the tracked mark resolved.
    expect(adapter.listOpen()).toEqual([]);
    expect(editor.getText()).toContain("the cache key and the vault hash");
  });
});
