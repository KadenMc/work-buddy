import { Editor } from "@tiptap/core";
import { afterEach, describe, expect, it } from "vitest";
import * as Y from "yjs";

import { bootstrapCoworkYdoc } from "../documents/bootstrapCoworkYdoc";
import { buildEditorExtensions } from "../editor/extensions";
import { editProposal } from "../suggestions/__tests__/support";
import { createWbTrackedChangesAdapter } from "../suggestions/adapter";
import type {
  DecisionItem,
  ProposalInput,
  SittingVerb,
} from "../suggestions/types";
import { RecoverableDecisionApplyError } from "../rail/applyRecovery";
import { prepareCoworkSittingDocument } from "./sittingWorkspace";

const documents: Y.Doc[] = [];

afterEach(() => {
  for (const document of documents.splice(0)) document.destroy();
});

const seedDocument = async (markdown: string): Promise<Y.Doc> => {
  const initialized = await bootstrapCoworkYdoc(new TextEncoder().encode(markdown));
  if (!initialized.ok) throw new Error(initialized.message);
  const document = new Y.Doc();
  documents.push(document);
  Y.applyUpdate(document, initialized.snapshot);
  return document;
};

const decision = (
  proposal: ProposalInput,
  verb: SittingVerb,
  extras: Partial<DecisionItem> = {},
): DecisionItem => ({
  proposal_id: proposal.proposal_id,
  verb,
  canonical_sha256: proposal.canonical_sha256,
  ...extras,
});

const assertNoTrackedMarks = (snapshot: Uint8Array): void => {
  const document = new Y.Doc();
  Y.applyUpdate(document, snapshot);
  const editor = new Editor({
    extensions: buildEditorExtensions(document),
    editable: false,
  });
  const trackedMarks: string[] = [];
  editor.state.doc.descendants((node) => {
    for (const mark of node.marks) {
      if (
        mark.type.name === "insertion" ||
        mark.type.name === "deletion" ||
        mark.type.name === "modification"
      ) {
        trackedMarks.push(mark.type.name);
      }
    }
    return true;
  });
  expect(trackedMarks).toEqual([]);
  editor.destroy();
  document.destroy();
};

describe("prepareCoworkSittingDocument", () => {
  it("materializes a confirmed modification directly without tracked marks", async () => {
    const document = await seedDocument("The quick brown fox");
    const proposal = editProposal("prop-1", "quick", "slow", {
      prefix: "The ",
      suffix: " brown",
    });

    const prepared = await prepareCoworkSittingDocument(
      document,
      [decision(proposal, "confirm")],
      [proposal],
      1,
    );

    expect(prepared.commit.rendered_markdown).toBe("The slow brown fox");
    assertNoTrackedMarks(prepared.commit.snapshot);
    prepared.dispose();
  });

  it("keeps the live document untouched while preparing the server commit", async () => {
    const document = await seedDocument("The quick brown fox");
    const proposal = editProposal("prop-1", "quick", "slow", {
      prefix: "The ",
      suffix: " brown",
    });
    let liveUpdates = 0;
    document.on("update", () => {
      liveUpdates += 1;
    });

    const prepared = await prepareCoworkSittingDocument(
      document,
      [decision(proposal, "confirm")],
      [proposal],
      1,
    );

    expect(liveUpdates).toBe(0);
    prepared.dispose();
  });

  it("fails closed when legacy suggestion artifacts contaminated canonical state", async () => {
    const document = await seedDocument("The quick brown fox");
    const proposal = editProposal("prop-1", "quick", "slow", {
      prefix: "The ",
      suffix: " brown",
    });
    const editor = new Editor({
      extensions: buildEditorExtensions(document),
    });
    const adapter = createWbTrackedChangesAdapter({ doc: document });
    adapter.attach(editor);
    adapter.ingestProposal(proposal);

    await expect(
      prepareCoworkSittingDocument(
        document,
        [decision(proposal, "confirm")],
        [proposal],
        1,
      ),
    ).rejects.toThrow(/refused to save noncanonical proposal projection/u);

    adapter.detach();
    editor.destroy();
  });

  it("materializes a confirmed empty replacement as deletion", async () => {
    const document = await seedDocument("The quick brown fox");
    const proposal = editProposal("prop-1", "quick ", "", { prefix: "The " });

    const prepared = await prepareCoworkSittingDocument(
      document,
      [decision(proposal, "confirm")],
      [proposal],
      1,
    );

    expect(prepared.commit.rendered_markdown).toBe("The brown fox");
    prepared.dispose();
  });

  it("uses a nonempty edit_confirm amendment instead of the proposal replacement", async () => {
    const document = await seedDocument("The quick brown fox");
    const proposal = editProposal("prop-1", "quick ", "slow ", { prefix: "The " });

    const prepared = await prepareCoworkSittingDocument(
      document,
      [decision(proposal, "edit_confirm", { amend_content: "swift " })],
      [proposal],
      1,
    );

    expect(prepared.commit.rendered_markdown).toBe("The swift brown fox");
    prepared.dispose();
  });

  it("uses an empty edit_confirm amendment as deletion", async () => {
    const document = await seedDocument("The quick brown fox");
    const proposal = editProposal("prop-1", "quick ", "slow ", { prefix: "The " });

    const prepared = await prepareCoworkSittingDocument(
      document,
      [decision(proposal, "edit_confirm", { amend_content: "" })],
      [proposal],
      1,
    );

    expect(prepared.commit.rendered_markdown).toBe("The brown fox");
    prepared.dispose();
  });

  it.each<SittingVerb>([
    "reject_plain",
    "reject_as_false",
    "reject_as_preference",
    "dismiss",
    "redirect",
    "defer",
    "endorse",
  ])("leaves canonical content unchanged for %s", async (verb) => {
    const document = await seedDocument("The quick brown fox");
    const proposal = editProposal("prop-1", "quick", "slow", {
      prefix: "The ",
      suffix: " brown",
    });

    const prepared = await prepareCoworkSittingDocument(
      document,
      [decision(proposal, verb)],
      [proposal],
      1,
    );

    expect(prepared.commit.rendered_markdown).toBe("The quick brown fox");
    prepared.dispose();
  });

  it("uses quote context to resolve the intended repeated passage", async () => {
    const document = await seedDocument(
      "Alpha target omega. Beta target gamma.",
    );
    const proposal = editProposal("prop-1", "target", "chosen", {
      prefix: "Beta ",
      suffix: " gamma.",
    });

    const prepared = await prepareCoworkSittingDocument(
      document,
      [decision(proposal, "confirm")],
      [proposal],
      1,
    );

    expect(prepared.commit.rendered_markdown).toBe(
      "Alpha target omega. Beta chosen gamma.",
    );
    prepared.dispose();
  });

  it("materializes a canonical Markdown hard-break proposal", async () => {
    const document = await seedDocument("The methods  \r\nsection follows.");
    const proposal = editProposal(
      "prop-hard-break",
      "methods  \r\nsection",
      "methods section",
      { prefix: "The ", suffix: " follows." },
    );

    const prepared = await prepareCoworkSittingDocument(
      document,
      [decision(proposal, "confirm")],
      [proposal],
      1,
    );

    expect(prepared.commit.rendered_markdown).toBe("The methods section follows.");
    prepared.dispose();
  });

  it("materializes a hard break inside a Markdown blockquote", async () => {
    const document = await seedDocument("> Keep the idea at  \r\n> the centre.");
    const proposal = editProposal(
      "prop-blockquote-break",
      "at  \r\n> the centre.",
      "at the centre.",
      { prefix: "idea ", suffix: "" },
    );

    const prepared = await prepareCoworkSittingDocument(
      document,
      [decision(proposal, "confirm")],
      [proposal],
      1,
    );

    expect(prepared.commit.rendered_markdown).toBe("> Keep the idea at the centre.");
    prepared.dispose();
  });

  it("fails closed when an admitted proposal is missing from the catalog", async () => {
    const document = await seedDocument("The quick brown fox");
    const proposal = editProposal("prop-1", "quick", "slow");

    const result = await prepareCoworkSittingDocument(
        document,
        [decision(proposal, "confirm")],
        [],
        1,
      ).catch((error: unknown) => error);

    expect(result).toBeInstanceOf(RecoverableDecisionApplyError);
    expect((result as RecoverableDecisionApplyError).recovery).toEqual({
      availableProposalIds: [],
      blockers: [
        expect.objectContaining({
          proposalId: "prop-1",
          reason: "proposal_unavailable",
        }),
      ],
    });
  });

  it("fails closed when the admitted canonical hash does not match", async () => {
    const document = await seedDocument("The quick brown fox");
    const proposal = editProposal("prop-1", "quick", "slow");

    const result = await prepareCoworkSittingDocument(
        document,
        [
          {
            ...decision(proposal, "confirm"),
            canonical_sha256: "stale-canonical-hash",
          },
        ],
        [proposal],
        1,
      ).catch((error: unknown) => error);

    expect(result).toBeInstanceOf(RecoverableDecisionApplyError);
    expect((result as RecoverableDecisionApplyError).recovery.blockers).toEqual([
      expect.objectContaining({
        proposalId: "prop-1",
        reason: "proposal_changed",
      }),
    ]);
  });

  it("fails closed when an admitted quote anchor is unresolved", async () => {
    const document = await seedDocument("The quick brown fox");
    const proposal = editProposal("prop-1", "missing", "slow");

    const result = await prepareCoworkSittingDocument(
        document,
        [decision(proposal, "confirm")],
        [proposal],
        1,
      ).catch((error: unknown) => error);

    expect(result).toBeInstanceOf(RecoverableDecisionApplyError);
    expect((result as RecoverableDecisionApplyError).recovery.blockers).toEqual([
      expect.objectContaining({
        proposalId: "prop-1",
        reason: "passage_unavailable",
      }),
    ]);
  });

  it("identifies overlapping edits and preserves the disjoint available subset", async () => {
    const document = await seedDocument("abcdef");
    const first = editProposal("prop-1", "bcd", "one");
    const second = editProposal("prop-2", "cde", "two");
    const third = editProposal("prop-3", "f", "three");

    const result = await prepareCoworkSittingDocument(
        document,
        [
          decision(first, "confirm"),
          decision(second, "confirm"),
          decision(third, "confirm"),
        ],
        [first, second, third],
        1,
      ).catch((error: unknown) => error);

    expect(result).toBeInstanceOf(RecoverableDecisionApplyError);
    expect((result as RecoverableDecisionApplyError).recovery).toEqual({
      availableProposalIds: ["prop-3"],
      blockers: [
        expect.objectContaining({
          proposalId: "prop-1",
          reason: "conflicts_with_selected_edit",
          relatedProposalIds: ["prop-2"],
        }),
        expect.objectContaining({
          proposalId: "prop-2",
          reason: "conflicts_with_selected_edit",
          relatedProposalIds: ["prop-1"],
        }),
      ],
    });
  });

  it("applies only the admitted subset of the authoritative catalog", async () => {
    const document = await seedDocument("first and second");
    const first = editProposal("prop-1", "first", "one");
    const second = editProposal("prop-2", "second", "two");

    const prepared = await prepareCoworkSittingDocument(
      document,
      [decision(second, "confirm")],
      [first, second],
      1,
    );

    expect(prepared.commit.rendered_markdown).toBe("first and two");
    prepared.dispose();
  });
});
