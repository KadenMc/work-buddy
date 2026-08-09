import { StrictMode } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";

import type {
  TruthClaimDetail,
  TruthClaimSummary,
  TruthClaimsSnapshot,
  TruthEditorIntegration,
  TruthRailProvider,
  TruthSelectionCapture,
} from "./contracts";
import { TruthAttentionFeed } from "./TruthAttentionFeed";
import { TruthPanel } from "./TruthPanel";
import { TruthStore } from "./store";

const connection = {
  expressionId: "expression-1",
  spanId: "span-1",
  documentId: "doc-1",
  documentTitle: "Draft",
  documentPath: "draft.md",
  role: "quote" as const,
  quote: "The document has a heading.",
  selector: {
    kind: "text_quote" as const,
    exact: "The document has a heading.",
    prefix: "",
    suffix: "",
    start: 0,
    end: 27,
  },
  currentDocument: true,
  claimCanonicalSha256: "canonical",
  createdAt: "2026-08-04T12:00:00Z",
  createdBy: { kind: "human", ref: "owner" },
};

const summary: TruthClaimSummary = {
  claimId: "claim-1",
  proposition: "The document has a heading.",
  claimKind: "fact",
  canonicalSha256: "canonical",
  scope: "store",
  baseStatus: "proposed",
  needsReview: false,
  health: "clean",
  healthReason: null,
  voided: false,
  redacted: false,
  validFrom: null,
  validTo: null,
  effectiveValidFrom: null,
  effectiveValidTo: null,
  evidenceCount: 1,
  connectionCount: 1,
  connections: [connection],
  createdAt: "2026-08-04T12:00:00Z",
  isFact: false,
  availableActions: ["confirm", "reject", "redact"],
};

const detail: TruthClaimDetail = {
  ...summary,
  structured: {},
  receipts: [{
    linkId: "link-1",
    spanId: "evidence-span-1",
    evidenceId: "evidence-1",
    evidenceKind: "source",
    quote: "Supporting text",
    sourceLocator: "Primary notes",
    trustClass: "human_authored",
    authorKind: "human",
    authorRef: "owner",
    active: true,
    spanSha256: "span-hash",
    contentSha256: "content-hash",
    mediaType: "text/plain",
    derivedFromStore: null,
    acquiredAt: "2026-08-04T12:00:00Z",
    acquisitionMethod: "fixture",
    spanRedactedAt: null,
    evidenceRedactedAt: null,
    integrity: {
      state: "valid",
      detail: null,
      locatorScheme: "file",
      verifiabilityClass: "snapshot",
      snapshotPresent: true,
    },
  }],
  lifecycle: [{
    eventId: "event-1",
    status: "proposed",
    at: "2026-08-04T12:00:00Z",
    actorKind: "agent",
    actorRef: "run-1",
    note: null,
  }],
  conflicts: [],
  derivations: [],
  support: {
    supportSpanIds: ["evidence-span-1"],
    usableSpanIds: ["evidence-span-1"],
    quarantinedOnly: false,
    agentAuthoredOnly: false,
    storeDerivedOnly: false,
  },
  premises: {
    localUnconfirmed: [],
    unresolvedUris: [],
    confirmed: true,
  },
  decisionBinding: {
    payloadSha256: "payload-hash",
    contextSha256: "context-hash",
    agentAuthoredOnly: false,
  },
};

const snapshot = (claims: readonly TruthClaimSummary[] = [summary]): TruthClaimsSnapshot => ({
  schema: "cowork-truth/v1",
  storeId: "store-1",
  documentId: "doc-1",
  scope: "document",
  filter: "all",
  claims,
  counts: {
    all: claims.length,
    facts: claims.filter((claim) => claim.isFact).length,
    proposed: claims.filter((claim) => claim.baseStatus === "proposed").length,
    needsReview: claims.filter((claim) => claim.needsReview).length,
    challenged: claims.filter((claim) => claim.baseStatus === "challenged").length,
    unconnected: claims.filter((claim) => claim.connectionCount === 0).length,
  },
  capabilities: {
    canObserve: true,
    canModify: true,
    canDecide: true,
    allowedClaimKinds: ["fact", "decision"],
    mutationUnavailableReason: null,
  },
  readOnly: false,
  nextOffset: null,
});

const capturedSelection: TruthSelectionCapture = {
  schema: "wb.cowork.truth-selection/v1",
  captureId: "capture-1",
  storeId: "store-1",
  documentId: "doc-1",
  structuredHeadSha256: "head",
  ydocGenerationSha256: "generation",
  projectionSha256: "projection",
  label: "Selection",
  wordCount: 5,
  selector: {
    kind: "text_quote",
    exact: "The document has a heading.",
    prefix: "",
    suffix: "",
    start: 0,
    end: 27,
  },
};

function setupProvider(
  claims: readonly TruthClaimSummary[] = [summary],
  claimDetail: TruthClaimDetail = detail,
) {
  const source = snapshot(claims);
  const provider: TruthRailProvider = {
    load: vi.fn(async (query) => ({ ...source, scope: query.scope, filter: query.filter })),
    loadClaim: vi.fn(async () => claimDetail),
    subscribe: vi.fn(() => () => undefined),
    proposeClaim: vi.fn(async () => ({ ok: true, claimId: "claim-1", claimCreated: true, expressionId: "expression-1", expressionCreated: true, status: "proposed" })),
    connectClaim: vi.fn(async () => ({ ok: true, claimId: "claim-1", claimCreated: false, expressionId: "expression-1", expressionCreated: true, status: "proposed" })),
    decideClaim: vi.fn(async () => ({ ok: true, claimId: "claim-1", claimCreated: false, expressionId: null, expressionCreated: false, status: "confirmed" })),
  };
  return provider;
}

const setupEditor = () => {
  const captureSelection = vi.fn(async () => capturedSelection);
  const revealPassage = vi.fn();
  const focusClaim = vi.fn();
  const editor: TruthEditorIntegration = { captureSelection, revealPassage, focusClaim };
  return { editor, captureSelection, revealPassage, focusClaim };
};

describe("TruthPanel", () => {
  it("does not clear an editor focus it did not establish", async () => {
    const { editor, focusClaim } = setupEditor();
    render(
      <TruthPanel
        provider={setupProvider()}
        storeId="store-1"
        documentId="doc-1"
        store={new TruthStore()}
        editor={editor}
      />,
    );

    await screen.findByText(summary.proposition);
    expect(focusClaim).not.toHaveBeenCalled();
  });

  it("keeps passive claim focus separate from explicit document navigation", async () => {
    const provider = setupProvider();
    const { editor, revealPassage, focusClaim } = setupEditor();
    render(<TruthPanel provider={provider} storeId="store-1" documentId="doc-1" store={new TruthStore()} editor={editor} />);

    const card = await screen.findByRole("listitem");
    fireEvent.click(card);
    await screen.findByRole("heading", { name: summary.proposition });

    expect(focusClaim).toHaveBeenCalledWith("claim-1");
    expect(revealPassage).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Show in document" }));
    expect(revealPassage).toHaveBeenCalledWith(connection);
  });

  it("shows claim health separately from the append-only lifecycle status", async () => {
    const redacted = {
      ...summary,
      baseStatus: "confirmed" as const,
      health: "redacted" as const,
      redacted: true,
      isFact: false,
    };
    render(
      <TruthPanel
        provider={setupProvider([redacted], { ...detail, ...redacted })}
        storeId="store-1"
        documentId="doc-1"
        store={new TruthStore()}
      />,
    );

    expect(await screen.findByText("Confirmed · Redacted")).toBeVisible();
  });

  it("flushes scroll continuity before semantic scope/filter changes", async () => {
    const provider = setupProvider();
    const onScrollContainerWillDetach = vi.fn();
    const scrollContainerRef = vi.fn();
    render(
      <TruthPanel
        provider={provider}
        storeId="store-1"
        documentId="doc-1"
        store={new TruthStore()}
        scroll={{ onScrollContainerWillDetach, scrollContainerRef }}
      />,
    );
    await screen.findByText(summary.proposition);
    await userEvent.click(screen.getByRole("button", { name: "Folder" }));
    await waitFor(() => expect(provider.load).toHaveBeenCalledWith({ scope: "folder", filter: "all" }));
    await userEvent.click(screen.getByRole("button", { name: /Needs review/ }));
    expect(onScrollContainerWillDetach).toHaveBeenCalledTimes(2);
    await waitFor(() =>
      expect(
        scrollContainerRef.mock.calls.filter(
          ([element]) => element instanceof HTMLElement,
        ).length,
      ).toBeGreaterThanOrEqual(3),
    );
  });

  it("captures an exact selection before proposing and connecting a claim", async () => {
    const provider = setupProvider();
    const { editor, captureSelection } = setupEditor();
    render(<TruthPanel provider={provider} storeId="store-1" documentId="doc-1" store={new TruthStore()} editor={editor} />);
    await screen.findByText(summary.proposition);

    await userEvent.click(screen.getByRole("button", { name: "Propose from selection" }));
    expect(await screen.findByText("Selected passage")).toBeVisible();
    expect(captureSelection).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Propose and connect" }));
    await waitFor(() => expect(provider.proposeClaim).toHaveBeenCalledWith({
      capture: capturedSelection,
      proposition: capturedSelection.selector.exact,
      claimKind: "fact",
      role: "quote",
    }));
  });

  it("starts one selection capture when StrictMode replays the composer", async () => {
    const provider = setupProvider();
    const { editor, captureSelection } = setupEditor();
    render(
      <StrictMode>
        <TruthPanel
          provider={provider}
          storeId="store-1"
          documentId="doc-1"
          store={new TruthStore()}
          editor={editor}
        />
      </StrictMode>,
    );
    await screen.findByText(summary.proposition);

    await userEvent.click(
      screen.getByRole("button", { name: "Propose from selection" }),
    );

    expect(await screen.findByText("Selected passage")).toBeVisible();
    expect(captureSelection).toHaveBeenCalledTimes(1);
    expect(
      screen.queryByText("Co-work is already capturing another document target."),
    ).toBeNull();
  });

  it("connects the captured selection to the explicitly chosen existing claim", async () => {
    const provider = setupProvider();
    const { editor } = setupEditor();
    render(
      <TruthPanel
        provider={provider}
        storeId="store-1"
        documentId="doc-1"
        store={new TruthStore()}
        editor={editor}
      />,
    );
    await screen.findByText(summary.proposition);

    await userEvent.click(
      screen.getByRole("button", { name: "Connect selection" }),
    );
    expect(await screen.findByText("Selected passage")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Connect claim" }));

    await waitFor(() =>
      expect(provider.connectClaim).toHaveBeenCalledWith({
        capture: capturedSelection,
        claimId: "claim-1",
        role: "quote",
      }),
    );
  });

  it("announces an idempotent connection without claiming a change", async () => {
    const provider = setupProvider();
    vi.mocked(provider.connectClaim).mockResolvedValue({
      ok: true,
      claimId: "claim-1",
      claimCreated: false,
      expressionId: "expression-1",
      expressionCreated: false,
      status: "proposed",
    });
    const { editor } = setupEditor();
    render(<TruthPanel provider={provider} storeId="store-1" documentId="doc-1" store={new TruthStore()} editor={editor} />);
    await screen.findByText(summary.proposition);

    await userEvent.click(screen.getByRole("button", { name: "Connect selection" }));
    await screen.findByText("Selected passage");
    await userEvent.click(screen.getByRole("button", { name: "Connect claim" }));

    expect(await screen.findByText(
      "No change was needed: the selected passage was already connected to that claim.",
    )).toBeInTheDocument();
  });

  it("keeps an empty retired document observable without offering doomed mutations", async () => {
    const provider = setupProvider([]);
    vi.mocked(provider.load).mockImplementation(async (query) => ({
      ...snapshot([]),
      scope: query.scope,
      filter: query.filter,
      capabilities: {
        ...snapshot([]).capabilities,
        canModify: false,
        canDecide: false,
        mutationUnavailableReason:
          "This document is retired, so its Truth connections cannot change.",
      },
    }));
    const { editor } = setupEditor();

    render(
      <TruthPanel
        provider={provider}
        storeId="store-1"
        documentId="doc-1"
        store={new TruthStore()}
        editor={editor}
      />,
    );

    expect(await screen.findByText("No claims are connected to this document")).toBeVisible();
    expect(screen.getByText(/document is retired/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Propose from selection" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Connect selection" })).toBeNull();
  });

  it("binds a visible confirmation to the exact server decision hashes", async () => {
    const provider = setupProvider();
    render(<TruthPanel provider={provider} storeId="store-1" documentId="doc-1" store={new TruthStore({ selectedClaimId: "claim-1" })} />);
    await screen.findByRole("heading", { name: summary.proposition });
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm claim" }));
    await waitFor(() => expect(provider.decideClaim).toHaveBeenCalledWith({
      claimId: "claim-1",
      action: "confirm",
      expectedCanonicalSha256: "payload-hash",
      expectedContextSha256: "context-hash",
      gestureKind: "confirm",
    }));
  });

  it("keeps an in-flight claim decision visible until its result is known", async () => {
    const provider = setupProvider();
    let settleDecision: (() => void) | null = null;
    vi.mocked(provider.decideClaim).mockImplementation(
      () =>
        new Promise((resolve) => {
          settleDecision = () =>
            resolve({
              ok: true,
              claimId: "claim-1",
              claimCreated: false,
              expressionId: null,
              expressionCreated: false,
              status: "confirmed",
            });
        }),
    );
    render(
      <TruthPanel
        provider={provider}
        storeId="store-1"
        documentId="doc-1"
        store={new TruthStore({ selectedClaimId: "claim-1" })}
      />,
    );
    await screen.findByRole("heading", { name: summary.proposition });
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm claim" }));

    expect(screen.getByRole("button", { name: "Close" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();

    await act(async () => {
      settleDecision?.();
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Close" })).toBeEnabled(),
    );
  });

  it("retains visible data and warns when a background refresh fails", async () => {
    let invalidate: (() => void) | null = null;
    const provider = setupProvider();
    vi.mocked(provider.subscribe).mockImplementation((listener) => {
      invalidate = listener;
      return () => undefined;
    });
    vi.mocked(provider.load)
      .mockResolvedValueOnce(snapshot())
      .mockRejectedValueOnce(new Error("The Truth refresh failed."));

    render(
      <TruthPanel
        provider={provider}
        storeId="store-1"
        documentId="doc-1"
        store={new TruthStore()}
      />,
    );
    expect(await screen.findByText(summary.proposition)).toBeVisible();

    act(() => invalidate?.());

    expect(await screen.findByText("The Truth refresh failed.")).toBeVisible();
    expect(screen.getByText(summary.proposition)).toBeVisible();
  });

  it("makes every expression explicit instead of jumping to an arbitrary first passage", async () => {
    const secondConnection = {
      ...connection,
      expressionId: "expression-2",
      spanId: "span-2",
      quote: "A second expression of the same claim.",
      selector: {
        ...connection.selector,
        exact: "A second expression of the same claim.",
        start: 50,
        end: 88,
      },
    };
    const multiSummary = {
      ...summary,
      connectionCount: 2,
      connections: [connection, secondConnection],
    };
    const multiDetail = {
      ...detail,
      connectionCount: 2,
      connections: [connection, secondConnection],
    };
    const { editor, revealPassage } = setupEditor();
    render(
      <TruthPanel
        provider={setupProvider([multiSummary], multiDetail)}
        storeId="store-1"
        documentId="doc-1"
        store={new TruthStore()}
        editor={editor}
      />,
    );

    expect(await screen.findByText("2 passages in this document")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Show in document" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: summary.proposition }));
    const details = await screen.findByRole("region", { name: "Claim details" });
    const passageButtons = within(details).getAllByRole("button", {
      name: "Show in document",
    });
    expect(passageButtons).toHaveLength(2);
    await userEvent.click(passageButtons[1]);
    expect(revealPassage).toHaveBeenCalledWith(secondConnection);
  });

  it("offers an explicit navigation action for a passage in another document", async () => {
    const foreignConnection = {
      ...connection,
      expressionId: "expression-other",
      spanId: "span-other",
      documentId: "doc-2",
      documentTitle: "Related draft",
      documentPath: "related-draft.md",
      quote: "The related draft expresses the same claim.",
      selector: {
        ...connection.selector,
        exact: "The related draft expresses the same claim.",
      },
      currentDocument: false,
    };
    const folderSummary = {
      ...summary,
      connectionCount: 2,
      connections: [connection, foreignConnection],
    };
    const folderDetail = {
      ...detail,
      connectionCount: 2,
      connections: [connection, foreignConnection],
    };
    const { editor, revealPassage } = setupEditor();
    render(
      <TruthPanel
        provider={setupProvider([folderSummary], folderDetail)}
        storeId="store-1"
        documentId="doc-1"
        store={new TruthStore({ scope: "folder" })}
        editor={editor}
      />,
    );

    await userEvent.click(await screen.findByRole("button", {
      name: summary.proposition,
    }));
    const details = await screen.findByRole("region", { name: "Claim details" });
    await userEvent.click(within(details).getByRole("button", {
      name: "Open and show passage",
    }));

    expect(revealPassage).toHaveBeenCalledWith(foreignConnection);
  });

  it("moves focus into a claim and restores it to the originating card", async () => {
    render(
      <TruthPanel
        provider={setupProvider()}
        storeId="store-1"
        documentId="doc-1"
        store={new TruthStore()}
      />,
    );
    const claimButton = await screen.findByRole("button", {
      name: summary.proposition,
    });
    await userEvent.click(claimButton);
    const heading = await screen.findByRole("heading", {
      name: summary.proposition,
    });
    await waitFor(() => expect(heading).toHaveFocus());
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: summary.proposition }),
      ).toHaveFocus(),
    );
  });

  it("has no automated accessibility violations in the Truth composition", async () => {
    const { container } = render(
      <TruthPanel
        provider={setupProvider()}
        storeId="store-1"
        documentId="doc-1"
        store={new TruthStore()}
      />,
    );
    await screen.findByText(summary.proposition);
    await expectNoAccessibilityViolations(container);
  });
});

describe("TruthAttentionFeed", () => {
  it("routes only claims needing attention and exposes no decision verbs", async () => {
    const confirmed = { ...summary, claimId: "claim-2", proposition: "Settled fact", baseStatus: "confirmed" as const, isFact: true, availableActions: ["redact" as const] };
    const provider = setupProvider([summary, confirmed]);
    const onOpenClaim = vi.fn();
    render(<TruthAttentionFeed provider={provider} onOpenClaim={onOpenClaim} />);

    await userEvent.click(await screen.findByRole("button", { name: /The document has a heading/ }));
    expect(onOpenClaim).toHaveBeenCalledWith("claim-1");
    expect(screen.queryByText("Settled fact")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
  });

  it("cross-lists every actionable claim rather than silently capping the inbox", async () => {
    const claims = Array.from({ length: 7 }, (_, index) => ({
      ...summary,
      claimId: `claim-${String(index + 1)}`,
      proposition: `Actionable claim ${String(index + 1)}`,
    }));
    render(
      <TruthAttentionFeed
        provider={setupProvider(claims)}
        onOpenClaim={vi.fn()}
      />,
    );
    expect(await screen.findAllByRole("button", { name: /Actionable claim/ })).toHaveLength(7);
  });
});
