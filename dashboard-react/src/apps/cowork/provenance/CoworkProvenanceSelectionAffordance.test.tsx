import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Editor } from "@tiptap/core";
import { afterEach, describe, expect, it, vi } from "vitest";

import { resolveQuoteAnchor } from "../suggestions/anchor";
import { makeSuggestionEditor } from "../suggestions/__tests__/support";
import type {
  ProvenanceAttestation,
  ProvenanceData,
  ProvenanceProvider,
  ProvenanceSelectionAction,
  ProvenanceTarget,
} from "./view/contracts";
import {
  CoworkProvenanceSelectionAffordance,
  type CoworkProvenanceSelectionAffordanceProps,
} from "./CoworkProvenanceSelectionAffordance";

const ACTOR = {
  kind: "human",
  ref: "dashboard-user",
  identity_status: "local_actor_ref",
} as const;
const HEAD = "a".repeat(64);
const CONTENT = "<p>Make this sentence precise and clear enough.</p>";

let editor: Editor | undefined;

afterEach(() => {
  editor?.destroy();
  editor = undefined;
});

const record = (
  overrides: Partial<ProvenanceAttestation> = {},
): ProvenanceAttestation => ({
  attestationId: "attestation-1",
  at: "2026-08-12T12:00:00Z",
  assertedBy: { kind: "human", ref: ACTOR.ref, meta: null },
  scope: {
    kind: "document_span",
    documentVersionId: null,
    documentSpanId: "span-1",
    structuredHeadSha256: HEAD,
  },
  authorship: { kind: "ai", contributors: [] },
  humanReview: { status: "not_reviewed", reviewers: [] },
  source: { kind: "direct_entry" },
  basis: { kind: "user_attestation", ref: null },
  supersedesId: null,
  canonicalSha256: "b".repeat(64),
  ...overrides,
});

const target = (
  overrides: Partial<ProvenanceTarget> = {},
): ProvenanceTarget => {
  const attestation = record();
  return {
    projectionId: "document_span:span-1",
    target: {
      kind: "document_span",
      documentVersionId: null,
      documentSpanId: "span-1",
      structuredHeadSha256: HEAD,
      currentness: "current",
    },
    span: { exact: "precise", prefix: "", suffix: "" },
    effectiveAttestations: [attestation],
    effectiveAttestation: attestation,
    resolution: "resolved",
    reviewEligibility: "eligible",
    issue: null,
    history: [attestation],
    ...overrides,
  };
};

const data = (spans: readonly ProvenanceTarget[] = []): ProvenanceData => ({
  schema: "cowork-provenance-view/v1",
  currentStructuredHeadSha256: HEAD,
  documentDefault: null,
  spans,
  history: spans.flatMap((item) => item.history),
  summary: {
    totalTargets: spans.length,
    currentSpanCount: spans.length,
    aiUnreviewedCount: spans.length,
    reviewedCount: 0,
    conflictedCount: 0,
    staleCount: 0,
    unrecorded: spans.length === 0,
  },
});

const provider = (value: ProvenanceData): ProvenanceProvider => ({
  load: vi.fn().mockResolvedValue({ state: "ready", data: value }),
  refresh: vi.fn().mockResolvedValue({ state: "ready", data: value }),
  subscribe: () => () => undefined,
  markReviewed: vi.fn().mockResolvedValue(undefined),
});

const select = (value: string): void => {
  if (editor === undefined) throw new Error("editor unavailable");
  const range = resolveQuoteAnchor(editor.state.doc, {
    exact: value,
    prefix: "",
    suffix: "",
  });
  if (range === null) throw new Error(`selection not found: ${value}`);
  act(() => editor!.commands.setTextSelection(range));
};

const mount = (
  value: ProvenanceData,
  options: {
    readonly onAction?: CoworkProvenanceSelectionAffordanceProps["onAction"];
    readonly active?: boolean;
  } = {},
) => {
  editor = makeSuggestionEditor({ content: CONTENT });
  const source = provider(value);
  const onRecord = vi
    .fn<CoworkProvenanceSelectionAffordanceProps["onRecord"]>()
    .mockResolvedValue(undefined);
  const onAction =
    options.onAction ??
    vi.fn<CoworkProvenanceSelectionAffordanceProps["onAction"]>();
  const rendered = render(
    <CoworkProvenanceSelectionAffordance
      editor={editor}
      active={options.active ?? true}
      provider={source}
      currentUserIdentity={ACTOR}
      onRecord={onRecord}
      onAction={onAction}
    />,
  );
  return { source, onRecord, onAction, unmount: rendered.unmount };
};

describe("CoworkProvenanceSelectionAffordance", () => {
  it("offers Record provenance for uncovered text and freezes the selected anchor", async () => {
    const { onRecord } = mount(data());
    select("precise");

    await userEvent.click(
      await screen.findByRole("button", { name: "Record provenance" }),
    );
    const dialog = screen.getByRole("dialog", { name: "Record provenance" });
    expect(dialog).toHaveTextContent("precise");
    expect(within(dialog).getByLabelText("Selected passage")).toHaveTextContent(
      "precise",
    );

    select("clear");
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Record provenance" }),
    );

    await waitFor(() => expect(onRecord).toHaveBeenCalledOnce());
    expect(onRecord.mock.calls[0]![0]).toMatchObject({ exact: "precise" });
  });

  it("routes an eligible AI passage to stable review detail without mutating", async () => {
    const onAction =
      vi.fn<CoworkProvenanceSelectionAffordanceProps["onAction"]>();
    const source = provider(data([target()]));
    editor = makeSuggestionEditor({ content: CONTENT });
    render(
      <CoworkProvenanceSelectionAffordance
        editor={editor}
        active
        provider={source}
        currentUserIdentity={ACTOR}
        onRecord={vi
          .fn<CoworkProvenanceSelectionAffordanceProps["onRecord"]>()
          .mockResolvedValue(undefined)}
        onAction={onAction}
      />,
    );
    select("precise");
    await userEvent.click(
      await screen.findByRole("button", { name: "Mark as reviewed" }),
    );

    const action = onAction.mock.calls[0]![0] as ProvenanceSelectionAction;
    expect(action).toMatchObject({
      intent: "review",
      targetIds: ["document_span:span-1"],
      reviewer: {
        ref: ACTOR.ref,
        identityStatus: ACTOR.identity_status,
      },
    });
    expect(source.markReviewed).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("uses View provenance when the selection is only part of a larger review target", async () => {
    const onAction =
      vi.fn<CoworkProvenanceSelectionAffordanceProps["onAction"]>();
    mount(data([target()]), { onAction });
    select("recis");

    await userEvent.click(
      await screen.findByRole("button", { name: "View provenance" }),
    );
    expect(onAction).toHaveBeenCalledWith(
      expect.objectContaining({
        intent: "view",
        targetIds: ["document_span:span-1"],
      }),
    );
  });

  it("does not reuse a review action identity after the affordance remounts", async () => {
    const firstAction =
      vi.fn<CoworkProvenanceSelectionAffordanceProps["onAction"]>();
    const first = mount(data([target()]), { onAction: firstAction });
    select("precise");
    await userEvent.click(
      await screen.findByRole("button", { name: "Mark as reviewed" }),
    );
    const firstRequestId = firstAction.mock.calls[0]![0].requestId;
    first.unmount();
    editor?.destroy();
    editor = undefined;

    const secondAction =
      vi.fn<CoworkProvenanceSelectionAffordanceProps["onAction"]>();
    mount(data([target()]), { onAction: secondAction });
    select("precise");
    await userEvent.click(
      await screen.findByRole("button", { name: "Mark as reviewed" }),
    );

    expect(secondAction.mock.calls[0]![0].requestId).toBeGreaterThan(
      firstRequestId,
    );
  });

  it("offers review when another person reviewed the target but the current user did not", async () => {
    const otherReviewer = {
      kind: "human" as const,
      ref: "another-reviewer",
      label: null,
      identityStatus: "local_actor_ref" as const,
    };
    const reviewed = record({
      humanReview: { status: "reviewed", reviewers: [otherReviewer] },
    });
    const onAction =
      vi.fn<CoworkProvenanceSelectionAffordanceProps["onAction"]>();
    mount(
      data([
        target({
          effectiveAttestations: [reviewed],
          effectiveAttestation: reviewed,
          history: [reviewed],
          reviewEligibility: "already_reviewed",
        }),
      ]),
      { onAction },
    );
    select("precise");

    await userEvent.click(
      await screen.findByRole("button", { name: "Mark as reviewed" }),
    );
    expect(onAction).toHaveBeenCalledWith(
      expect.objectContaining({
        intent: "review",
        targetIds: ["document_span:span-1"],
      }),
    );
  });

  it("uses View provenance when the current user already reviewed the target", async () => {
    const currentReviewer = {
      kind: "human" as const,
      ref: ACTOR.ref,
      label: null,
      identityStatus: ACTOR.identity_status,
    };
    const reviewed = record({
      humanReview: { status: "reviewed", reviewers: [currentReviewer] },
    });
    mount(
      data([
        target({
          effectiveAttestations: [reviewed],
          effectiveAttestation: reviewed,
          history: [reviewed],
          reviewEligibility: "already_reviewed",
        }),
      ]),
    );
    select("precise");

    expect(
      await screen.findByRole("button", { name: "View provenance" }),
    ).toBeVisible();
  });

  it("routes only fully selected targets that still need this user's review", async () => {
    const currentReviewer = {
      kind: "human" as const,
      ref: ACTOR.ref,
      label: null,
      identityStatus: ACTOR.identity_status,
    };
    const alreadyReviewed = record({
      attestationId: "attestation-2",
      scope: {
        kind: "document_span",
        documentVersionId: null,
        documentSpanId: "span-2",
        structuredHeadSha256: HEAD,
      },
      humanReview: { status: "reviewed", reviewers: [currentReviewer] },
    });
    const clearTarget = target({
      projectionId: "document_span:span-2",
      target: {
        ...target().target,
        documentSpanId: "span-2",
      },
      span: { exact: "clear", prefix: "", suffix: "" },
      effectiveAttestations: [alreadyReviewed],
      effectiveAttestation: alreadyReviewed,
      reviewEligibility: "already_reviewed",
      history: [alreadyReviewed],
    });
    const onAction =
      vi.fn<CoworkProvenanceSelectionAffordanceProps["onAction"]>();
    mount(data([target(), clearTarget]), { onAction });
    select("precise and clear");

    await userEvent.click(
      await screen.findByRole("button", { name: "Mark as reviewed" }),
    );
    expect(onAction).toHaveBeenCalledWith(
      expect.objectContaining({
        intent: "review",
        targetIds: ["document_span:span-1"],
      }),
    );
  });

  it("offers review for contained targets while leaving partial targets untouched", async () => {
    const partialRecord = record({
      attestationId: "attestation-2",
      scope: {
        kind: "document_span",
        documentVersionId: null,
        documentSpanId: "span-2",
        structuredHeadSha256: HEAD,
      },
    });
    const partialTarget = target({
      projectionId: "document_span:span-2",
      target: {
        ...target().target,
        documentSpanId: "span-2",
      },
      span: { exact: "clear enough", prefix: "", suffix: "" },
      effectiveAttestations: [partialRecord],
      effectiveAttestation: partialRecord,
      history: [partialRecord],
    });
    const onAction =
      vi.fn<CoworkProvenanceSelectionAffordanceProps["onAction"]>();
    mount(data([target(), partialTarget]), { onAction });
    select("precise and clear");

    await userEvent.click(
      await screen.findByRole("button", { name: "Mark as reviewed" }),
    );
    expect(onAction).toHaveBeenCalledWith(
      expect.objectContaining({
        intent: "review",
        targetIds: ["document_span:span-1"],
      }),
    );
  });

  it("offers View provenance for a healthy passage without a review action", async () => {
    const human = record({
      authorship: {
        kind: "human",
        contributors: [
          {
            kind: "human",
            ref: ACTOR.ref,
            label: null,
            identityStatus: "local_actor_ref",
          },
        ],
      },
      humanReview: { status: "not_applicable", reviewers: [] },
    });
    const onAction =
      vi.fn<CoworkProvenanceSelectionAffordanceProps["onAction"]>();
    mount(
      data([
        target({
          effectiveAttestations: [human],
          effectiveAttestation: human,
          history: [human],
          reviewEligibility: "not_ai_authored",
        }),
      ]),
      { onAction },
    );
    select("precise");
    await userEvent.click(
      await screen.findByRole("button", { name: "View provenance" }),
    );
    expect(onAction).toHaveBeenCalledWith(
      expect.objectContaining({ intent: "view" }),
    );
  });

  it("treats text beyond a stale whole-document record as uncovered", async () => {
    const wholeDocumentRecord = record({
      scope: {
        kind: "document_version",
        documentVersionId: "version-1",
        documentSpanId: null,
        structuredHeadSha256: "c".repeat(64),
      },
    });
    const staleDefault: ProvenanceTarget = {
      ...target(),
      projectionId: "document_version:version-1",
      target: {
        kind: "document_version",
        documentVersionId: "version-1",
        documentSpanId: null,
        structuredHeadSha256: "c".repeat(64),
        currentness: "stale",
      },
      span: null,
      effectiveAttestations: [wholeDocumentRecord],
      effectiveAttestation: wholeDocumentRecord,
      reviewEligibility: "stale_target",
      history: [wholeDocumentRecord],
    };
    mount({
      ...data(),
      documentDefault: staleDefault,
      history: [wholeDocumentRecord],
    });
    select("precise");
    expect(
      await screen.findByRole("button", { name: "Record provenance" }),
    ).toBeVisible();
  });

  it("offers this user review for a whole-document record reviewed only by someone else", async () => {
    const wholeDocumentRecord = record({
      scope: {
        kind: "document_version",
        documentVersionId: "version-1",
        documentSpanId: null,
        structuredHeadSha256: HEAD,
      },
      humanReview: {
        status: "reviewed",
        reviewers: [
          {
            kind: "human",
            ref: "other-reviewer",
            label: "Other reviewer",
            identityStatus: "local_actor_ref",
          },
        ],
      },
    });
    const wholeDocumentTarget: ProvenanceTarget = {
      projectionId: "document_version:version-1",
      target: {
        kind: "document_version",
        documentVersionId: "version-1",
        documentSpanId: null,
        structuredHeadSha256: HEAD,
        currentness: "current",
      },
      span: null,
      effectiveAttestations: [wholeDocumentRecord],
      effectiveAttestation: wholeDocumentRecord,
      resolution: "resolved",
      reviewEligibility: "already_reviewed",
      issue: null,
      history: [wholeDocumentRecord],
    };
    const { onAction } = mount({
      ...data(),
      documentDefault: wholeDocumentTarget,
      history: [wholeDocumentRecord],
    });

    select("precise");
    expect(
      await screen.findByRole("button", { name: "View provenance" }),
    ).toBeVisible();

    select("Make this sentence precise and clear enough.");
    await userEvent.click(
      await screen.findByRole("button", { name: "Mark as reviewed" }),
    );
    expect(onAction).toHaveBeenCalledWith(
      expect.objectContaining({
        intent: "review",
        targetIds: ["document_version:version-1"],
        coversWholeDocument: true,
        reviewer: {
          ref: ACTOR.ref,
          identityStatus: ACTOR.identity_status,
        },
      }),
    );
  });

  it("routes stale and multiply covered selections to Inspect provenance", async () => {
    const stale = target({
      target: { ...target().target, currentness: "requires_reanchor" },
      reviewEligibility: "stale_target",
    });
    const duplicate = target({
      projectionId: "document_span:span-2",
      target: { ...target().target, documentSpanId: "span-2" },
    });
    const onAction =
      vi.fn<CoworkProvenanceSelectionAffordanceProps["onAction"]>();
    editor = makeSuggestionEditor({ content: CONTENT });
    const firstProvider = provider(data([stale]));
    const { rerender } = render(
      <CoworkProvenanceSelectionAffordance
        editor={editor}
        active
        provider={firstProvider}
        currentUserIdentity={ACTOR}
        onRecord={vi
          .fn<CoworkProvenanceSelectionAffordanceProps["onRecord"]>()
          .mockResolvedValue(undefined)}
        onAction={onAction}
      />,
    );
    select("precise");
    expect(
      await screen.findByRole("button", { name: "Inspect provenance" }),
    ).toBeVisible();

    const secondProvider = provider(data([target(), duplicate]));
    rerender(
      <CoworkProvenanceSelectionAffordance
        editor={editor!}
        active
        provider={secondProvider}
        currentUserIdentity={ACTOR}
        onRecord={vi
          .fn<CoworkProvenanceSelectionAffordanceProps["onRecord"]>()
          .mockResolvedValue(undefined)}
        onAction={onAction}
      />,
    );
    expect(
      await screen.findByRole("button", { name: "Inspect provenance" }),
    ).toBeVisible();
  });

  it("renders no provenance action outside the Provenance lens", () => {
    mount(data(), { active: false });
    select("precise");
    expect(screen.queryByRole("button")).toBeNull();
  });
});
