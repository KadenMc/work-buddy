import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  ProvenanceData,
  ProvenanceEditorIntegration,
  ProvenanceMutationBarrier,
  ProvenanceProvider,
  ProvenanceSelectionAction,
} from "./contracts";
import { ProvenancePanel } from "./ProvenancePanel";

const attestation = {
  attestationId: "attestation-1",
  at: "2026-08-12T12:00:00Z",
  assertedBy: { kind: "human", ref: "user-1", meta: null },
  scope: {
    kind: "document_span" as const,
    documentVersionId: null,
    documentSpanId: "span-1",
    structuredHeadSha256: "a".repeat(64),
  },
  authorship: {
    kind: "ai" as const,
    contributors: [
      {
        kind: "human" as const,
        ref: null,
        label: "Taylor",
        identityStatus: "claimed_name" as const,
      },
    ],
  },
  humanReview: { status: "not_reviewed" as const, reviewers: [] },
  source: {
    kind: "paste",
    provider: "clipboard",
    media_type: "text/plain",
    path: "notes/source.txt",
    sha256: "c".repeat(64),
    private_metadata: { must_not_render: true },
  },
  basis: { kind: "user_attestation", ref: null },
  supersedesId: null,
  canonicalSha256: "b".repeat(64),
};

const data: ProvenanceData = {
  schema: "cowork-provenance-view/v1",
  currentStructuredHeadSha256: "a".repeat(64),
  documentDefault: null,
  spans: [
    {
      projectionId: "document_span:span-1",
      target: {
        kind: "document_span",
        documentVersionId: null,
        documentSpanId: "span-1",
        structuredHeadSha256: "a".repeat(64),
        currentness: "current",
      },
      span: { exact: "AI passage", prefix: "", suffix: "" },
      effectiveAttestations: [attestation],
      effectiveAttestation: attestation,
      resolution: "resolved",
      reviewEligibility: "eligible",
      issue: null,
      history: [attestation],
    },
  ],
  history: [attestation],
  summary: {
    totalTargets: 1,
    currentSpanCount: 1,
    aiUnreviewedCount: 1,
    reviewedCount: 0,
    conflictedCount: 0,
    staleCount: 0,
    unrecorded: true,
  },
};

const provider = (): ProvenanceProvider => ({
  load: vi.fn().mockResolvedValue({ state: "ready", data }),
  refresh: vi.fn().mockResolvedValue({ state: "ready", data }),
  subscribe: () => () => undefined,
  markReviewed: vi.fn().mockResolvedValue(undefined),
});

const editor: ProvenanceEditorIntegration = {
  resolveTarget: () => ({
    state: "unique",
    documentOrder: 20,
    documentEnd: 30,
  }),
  isLocallyDirty: () => false,
  hasText: () => true,
  hasUncoveredText: () => true,
  focusTarget: vi.fn(),
  revealTarget: vi.fn(),
};

describe("ProvenancePanel", () => {
  it("starts with the provenance controls instead of repeating tab help", async () => {
    render(<ProvenancePanel provider={provider()} active editor={editor} />);

    expect(await screen.findByLabelText("Provenance summary")).toBeVisible();
    expect(screen.queryByText("Document provenance")).toBeNull();
    expect(
      screen.queryByText("How this text is attributed—and reviewed"),
    ).toBeNull();
    expect(
      screen.queryByText(/Authorship and human review are separate records/u),
    ).toBeNull();
  });

  it("subscribes once when a provider synchronously replays its retained snapshot", async () => {
    const source = provider();
    const load = vi.mocked(source.load);
    source.subscribe = vi.fn((listener) => {
      listener();
      return () => undefined;
    });

    render(<ProvenancePanel provider={source} active editor={editor} />);

    await screen.findByRole("button", { name: /AI passage/u });
    await waitFor(() => expect(load).toHaveBeenCalledTimes(1));
    expect(source.subscribe).toHaveBeenCalledTimes(1);
  });

  it("keeps selection passive and reserves navigation for Show in document", async () => {
    const source = provider();
    render(
      <ProvenancePanel
        provider={source}
        active
        editor={editor}
        mutationBarrier={{ runWithSynchronizedDocument: vi.fn() }}
      />,
    );
    const item = await screen.findByRole("button", { name: /AI passage/u });
    await userEvent.click(item);
    expect(editor.focusTarget).toHaveBeenCalledWith("document_span:span-1");
    expect(editor.revealTarget).not.toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: "Show in document" }),
    );
    expect(editor.revealTarget).toHaveBeenCalledWith("document_span:span-1");
    expect(screen.getByText(/no current provenance record/u)).toBeVisible();
  });

  it("holds the editor barrier across refresh, re-resolution, and mutation", async () => {
    const source = provider();
    let locked = false;
    const refresh = vi.mocked(source.refresh);
    refresh.mockImplementation(async () => {
      expect(locked).toBe(true);
      return { state: "ready", data };
    });
    vi.mocked(source.markReviewed).mockImplementation(async () => {
      expect(locked).toBe(true);
    });
    const barrier: ProvenanceMutationBarrier = {
      runWithSynchronizedDocument: async (operation) => {
        locked = true;
        try {
          return await operation({ structuredHeadSha256: "a".repeat(64) });
        } finally {
          locked = false;
        }
      },
    };
    render(
      <ProvenancePanel
        provider={source}
        active
        editor={editor}
        mutationBarrier={barrier}
      />,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: /AI passage/u }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Mark reviewed" }),
    );
    await waitFor(() => expect(source.markReviewed).toHaveBeenCalledOnce());
    expect(locked).toBe(false);
  });

  it("blocks the write when a fresh pull introduces an incompatible overlap", async () => {
    const source = provider();
    const peerRecord = {
      ...attestation,
      attestationId: "attestation-peer",
      scope: { ...attestation.scope, documentSpanId: "span-peer" },
      authorship: { kind: "human" as const, contributors: [] },
    };
    const peer = {
      ...data.spans[0]!,
      projectionId: "document_span:span-peer",
      target: { ...data.spans[0]!.target, documentSpanId: "span-peer" },
      span: { exact: "passage and", prefix: "AI ", suffix: " words" },
      effectiveAttestations: [peerRecord],
      effectiveAttestation: peerRecord,
      reviewEligibility: "not_ai_authored" as const,
      history: [peerRecord],
    };
    vi.mocked(source.refresh).mockResolvedValue({
      state: "ready",
      data: { ...data, spans: [data.spans[0]!, peer] },
    });
    const overlapEditor: ProvenanceEditorIntegration = {
      ...editor,
      resolveTarget: (anchor) =>
        anchor.exact === "AI passage"
          ? { state: "unique", documentOrder: 5, documentEnd: 20 }
          : { state: "unique", documentOrder: 10, documentEnd: 25 },
    };
    render(
      <ProvenancePanel
        provider={source}
        active
        editor={overlapEditor}
        mutationBarrier={{
          runWithSynchronizedDocument: (operation) =>
            operation({ structuredHeadSha256: "a".repeat(64) }),
        }}
      />,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: /AI passage/u }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Mark reviewed" }),
    );
    await screen.findByRole("alert");
    expect(source.markReviewed).not.toHaveBeenCalled();
  });

  it("does not claim an empty editor contains unrecorded text", async () => {
    render(
      <ProvenancePanel
        provider={{
          ...provider(),
          load: vi.fn().mockResolvedValue({
            state: "ready",
            data: { ...data, spans: [], history: [] },
          }),
        }}
        active
        editor={{ ...editor, hasText: () => false }}
      />,
    );
    expect(
      await screen.findByText("There is no text to map yet."),
    ).toBeVisible();
    expect(
      screen.queryByText(/Some text has no current provenance/u),
    ).toBeNull();
    expect(
      screen.queryByText("No provenance records match this filter."),
    ).toBeNull();
  });

  it("shows recent typing as recording instead of definitively unrecorded", async () => {
    render(
      <ProvenancePanel
        provider={provider()}
        active
        editor={{ ...editor, isLocallyDirty: () => true }}
        inputProvenancePending
      />,
    );

    expect(
      await screen.findByText(/Recording provenance for recent typing…/u),
    ).toBeVisible();
    expect(screen.getByText("Updating…")).toBeVisible();
    expect(
      screen.queryByText("Some text has no current provenance record"),
    ).toBeNull();
    expect(
      screen.queryByText("No provenance has been recorded for this document"),
    ).toBeNull();
    expect(
      screen.queryByText("No provenance records match this filter."),
    ).toBeNull();
  });

  it("gives a textful document with zero records one actionable empty state", async () => {
    render(
      <ProvenancePanel
        provider={{
          ...provider(),
          load: vi.fn().mockResolvedValue({
            state: "ready",
            data: { ...data, spans: [], history: [], documentDefault: null },
          }),
        }}
        active
        editor={editor}
      />,
    );

    expect(
      await screen.findByRole("heading", {
        name: "No provenance has been recorded for this document",
      }),
    ).toBeVisible();
    expect(screen.getByText(/Select existing text to record/u)).toBeVisible();
    expect(
      screen.queryByText(/Some text has no current provenance/u),
    ).toBeNull();
    expect(
      screen.queryByText("No provenance records match this filter."),
    ).toBeNull();
  });

  it("does not present a terminal empty state while recent typing is unsettled", async () => {
    render(
      <ProvenancePanel
        provider={{
          ...provider(),
          load: vi.fn().mockResolvedValue({
            state: "ready",
            data: { ...data, spans: [], history: [], documentDefault: null },
          }),
        }}
        active
        editor={{ ...editor, isLocallyDirty: () => true }}
      />,
    );

    expect(
      await screen.findByText(/Local edits are not yet represented/u),
    ).toBeVisible();
    expect(
      screen.queryByRole("heading", {
        name: "No provenance has been recorded for this document",
      }),
    ).toBeNull();
  });

  it("does not leave a zero-record filtered view blank", async () => {
    render(
      <ProvenancePanel
        provider={{
          ...provider(),
          load: vi.fn().mockResolvedValue({
            state: "ready",
            data: { ...data, spans: [], history: [], documentDefault: null },
          }),
        }}
        active
        editor={editor}
      />,
    );

    await userEvent.click(
      await screen.findByRole("button", { name: "Issues" }),
    );
    expect(
      screen.getByText("No provenance records match this filter."),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "All" })).toBeVisible();
  });

  it("opens stable target detail for a routed selection action", async () => {
    const selectionAction: ProvenanceSelectionAction = {
      requestId: 1,
      intent: "review",
      anchor: { exact: "AI passage", prefix: "", suffix: "" },
      from: 5,
      to: 15,
      targetIds: ["document_span:span-1"],
    };
    render(
      <ProvenancePanel
        provider={provider()}
        active
        editor={editor}
        mutationBarrier={{ runWithSynchronizedDocument: vi.fn() }}
        selectionAction={selectionAction}
      />,
    );

    expect(await screen.findByText("Actions")).toBeVisible();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /AI passage/u })).toHaveFocus(),
    );
    expect(screen.getByRole("button", { name: "Mark reviewed" })).toBeEnabled();
  });

  it("keeps Mark reviewed discoverable but disabled with a specific reason", async () => {
    const stale = {
      ...data.spans[0]!,
      target: {
        ...data.spans[0]!.target,
        currentness: "requires_reanchor" as const,
      },
      reviewEligibility: "stale_target" as const,
    };
    render(
      <ProvenancePanel
        provider={{
          ...provider(),
          load: vi.fn().mockResolvedValue({
            state: "ready",
            data: { ...data, spans: [stale] },
          }),
        }}
        active
        editor={editor}
        mutationBarrier={{ runWithSynchronizedDocument: vi.fn() }}
      />,
    );

    await userEvent.click(
      await screen.findByRole("button", { name: /AI passage/u }),
    );
    const action = screen.getByRole("button", { name: "Mark reviewed" });
    expect(action).toBeDisabled();
    expect(screen.getByText(/re-anchored for inspection/u)).toBeVisible();
    expect(action).toHaveAttribute("aria-describedby");
  });

  it("keeps global append-only history inspectable without a current head", async () => {
    render(
      <ProvenancePanel
        provider={{
          ...provider(),
          load: vi.fn().mockResolvedValue({
            state: "ready",
            data: {
              ...data,
              currentStructuredHeadSha256: null,
              spans: [],
            },
          }),
        }}
        active
        editor={editor}
      />,
    );
    const history = await screen.findByText("Complete provenance history (1)");
    await userEvent.click(history);
    expect(screen.getByText(/Passage · span-1/u)).toBeVisible();
  });

  it("includes a uniquely reanchored target in Issues", async () => {
    const reanchored = {
      ...data.spans[0]!,
      target: {
        ...data.spans[0]!.target,
        currentness: "requires_reanchor" as const,
      },
      reviewEligibility: "stale_target" as const,
    };
    render(
      <ProvenancePanel
        provider={{
          ...provider(),
          load: vi.fn().mockResolvedValue({
            state: "ready",
            data: { ...data, spans: [reanchored] },
          }),
        }}
        active
        editor={editor}
      />,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "Issues" }),
    );
    expect(
      screen.getByRole("button", { name: /AI passage/u }),
    ).toHaveTextContent("Reanchored for inspection");
  });

  it("labels an unavailable orphaned passage without document actions", async () => {
    const orphaned = {
      ...data.spans[0]!,
      target: {
        ...data.spans[0]!.target,
        currentness: "unavailable" as const,
      },
      span: null,
      effectiveAttestations: [attestation],
      effectiveAttestation: null,
      resolution: "conflicted" as const,
      reviewEligibility: "conflicted" as const,
      issue: {
        code: "missing_span_target",
        message: "The recorded provenance span is unavailable.",
      },
    };
    render(
      <ProvenancePanel
        provider={{
          ...provider(),
          load: vi.fn().mockResolvedValue({
            state: "ready",
            data: { ...data, spans: [orphaned] },
          }),
        }}
        active
        editor={editor}
        mutationBarrier={{ runWithSynchronizedDocument: vi.fn() }}
      />,
    );

    const item = await screen.findByRole("button", {
      name: /Unavailable passage/u,
    });
    expect(item).toHaveTextContent("Passage unavailable");
    expect(item).not.toHaveTextContent("Whole document");
    await userEvent.click(item);
    expect(
      screen.queryByRole("button", { name: "Show in document" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "Mark reviewed" })).toBeNull();
    expect(
      screen.getByText(/append-only history remains inspectable/u),
    ).toBeVisible();
  });

  it("shows bounded source fields and identity strength without dumping raw metadata", async () => {
    render(<ProvenancePanel provider={provider()} active editor={editor} />);
    await userEvent.click(
      await screen.findByRole("button", { name: /AI passage/u }),
    );

    expect(
      screen.getAllByText(/Taylor \(claimed name; not account-verified\)/u)
        .length,
    ).toBeGreaterThan(0);
    expect(screen.getAllByText(/Provider: clipboard/u).length).toBeGreaterThan(
      0,
    );
    expect(
      screen.getAllByText(/Media type: text\/plain/u).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByText(/Path: notes\/source\.txt/u).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText(/must_not_render/u)).toBeNull();
  });

  it("shows the bounded producing run for accepted AI proposals", async () => {
    const accepted = {
      ...attestation,
      source: {
        kind: "proposal_acceptance",
        proposal_id: "proposal-demo",
        acceptance_gesture_id: "gesture-demo",
        producer: {
          model: "gpt-5.6-sol",
          harness: "codex",
          surface: "mcp",
          session_id: "session-demo",
          private_metadata: "must not render",
        },
      },
      basis: { kind: "proposal_acceptance", ref: "gesture-demo" },
    };
    const acceptedData: ProvenanceData = {
      ...data,
      spans: [
        {
          ...data.spans[0]!,
          effectiveAttestations: [accepted],
          effectiveAttestation: accepted,
          history: [accepted],
        },
      ],
      history: [accepted],
    };
    render(
      <ProvenancePanel
        provider={{
          ...provider(),
          load: vi.fn().mockResolvedValue({
            state: "ready",
            data: acceptedData,
          }),
        }}
        active
        editor={editor}
      />,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: /AI passage/u }),
    );

    expect(
      screen.getAllByText(/Proposal: proposal-demo/u).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByText(/Producer model: gpt-5\.6-sol/u).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByText(/Producer harness: codex/u).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByText(/Producer run: session-demo/u).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText(/must not render/u)).toBeNull();
  });

  it("labels manually repaired legacy source as untracked", async () => {
    const untracked = {
      ...attestation,
      source: { kind: "legacy" },
    };
    render(
      <ProvenancePanel
        provider={{
          ...provider(),
          load: vi.fn().mockResolvedValue({
            state: "ready",
            data: {
              ...data,
              spans: [
                {
                  ...data.spans[0]!,
                  effectiveAttestations: [untracked],
                  effectiveAttestation: untracked,
                  history: [untracked],
                },
              ],
              history: [untracked],
            },
          }),
        }}
        active
        editor={editor}
      />,
    );

    await userEvent.click(
      await screen.findByRole("button", { name: /AI passage/u }),
    );
    expect(screen.getAllByText("Untracked / legacy").length).toBeGreaterThan(0);
  });

  it("blocks review and explains incompatible overlapping provenance", async () => {
    const human = {
      ...attestation,
      attestationId: "attestation-2",
      scope: { ...attestation.scope, documentSpanId: "span-2" },
      authorship: { kind: "human" as const, contributors: [] },
    };
    const overlapping = {
      ...data.spans[0]!,
      projectionId: "document_span:span-2",
      target: { ...data.spans[0]!.target, documentSpanId: "span-2" },
      span: { exact: "passage and", prefix: "AI ", suffix: " uncovered" },
      effectiveAttestations: [human],
      effectiveAttestation: human,
      reviewEligibility: "not_ai_authored" as const,
      history: [human],
    };
    const overlapEditor: ProvenanceEditorIntegration = {
      ...editor,
      resolveTarget: (anchor) =>
        anchor.exact === "AI passage"
          ? { state: "unique", documentOrder: 5, documentEnd: 20 }
          : { state: "unique", documentOrder: 10, documentEnd: 25 },
    };
    render(
      <ProvenancePanel
        provider={{
          ...provider(),
          load: vi.fn().mockResolvedValue({
            state: "ready",
            data: { ...data, spans: [data.spans[0]!, overlapping] },
          }),
        }}
        active
        editor={overlapEditor}
        mutationBarrier={{ runWithSynchronizedDocument: vi.fn() }}
      />,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: /AI passage/u }),
    );
    expect(
      screen.getByText(
        /Overlapping provenance records disagree on authorship/u,
      ),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Mark reviewed" }),
    ).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Issues" }));
    expect(
      screen.getAllByText("Conflicts with overlapping passage"),
    ).toHaveLength(2);
  });
});
