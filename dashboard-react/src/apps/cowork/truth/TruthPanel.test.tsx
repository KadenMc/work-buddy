import { StrictMode } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import { DashboardHelpProvider } from "../../../dashboard/help";
import type { ChatExecutionControl } from "../../../widget-library/chat";
import type { CoworkCapturedActionSnapshot } from "../targets";

import type {
  TruthAnalysisCandidate,
  TruthAnalysisCapabilities,
  TruthClaimDetail,
  TruthAnalysisProvider,
  TruthAnalysisRun,
  TruthClaimSummary,
  TruthClaimsSnapshot,
  TruthEditorIntegration,
  TruthRailProvider,
  TruthSelectionCapture,
} from "./contracts";
import { TruthAttentionFeed } from "./TruthAttentionFeed";
import { TruthPanel } from "./TruthPanel";
import { TruthClaimDetails } from "./TruthClaimDetails";
import { TruthStore } from "./store";
import { InMemoryTruthProvider } from "./InMemoryTruthProvider";
import { orderTruthPassages } from "./contracts";
import truthStyles from "./styles.css?raw";

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

const capturedAnalysisTarget: CoworkCapturedActionSnapshot = {
  schema: "wb.cowork.action-snapshot/v1",
  captureId: "analysis-capture-1",
  storeId: "store-1",
  documentId: "doc-1",
  capturedAt: "2026-08-09T12:00:00Z",
  editGeneration: 3,
  ydocGenerationSha256: "a".repeat(64),
  snapshotBase64: "c25hcHNob3Q=",
  snapshotSha256: "b".repeat(64),
  stateVectorBase64: "dmVjdG9y",
  stateVectorSha256: "c".repeat(64),
  structuredHeadSha256: "d".repeat(64),
  projectionMarkdown: "The document has a heading.",
  projectionSha256: "e".repeat(64),
  projectionReceiptId: "projection-receipt-1",
  target: {
    source: "current_selection",
    label: "Selected passage",
    wordCount: 5,
    proseMirrorRange: { from: 1, to: 28 },
    selector: {
      kind: "text_quote",
      exact: "The document has a heading.",
      prefix: "",
      suffix: "",
      start: 0,
      end: 27,
    },
    targetTextSha256: "f".repeat(64),
  },
};

const analysisRun: TruthAnalysisRun = {
  schema: "wb.cowork.truth-analysis-run/v1",
  analysisRunId: "analysis-run-1",
  storeId: "store-1",
  documentId: "doc-1",
  status: "queued",
  targetChoice: "current_selection",
  targetLabel: "Selected passage",
  capturedAt: "2026-08-09T12:00:00Z",
  structuredHeadSha256: "d".repeat(64),
  projectionSha256: "e".repeat(64),
  execution: {
    providerId: "claude-code",
    modelId: "sonnet",
    providerLabel: "Claude Code",
    modelLabel: "Sonnet",
  },
  candidates: [],
  sourceCoverage: [],
  limitations: [],
  error: null,
  createdAt: "2026-08-09T12:00:00Z",
  finishedAt: null,
};

const analysisCandidate: TruthAnalysisCandidate = {
  candidateId: "candidate-1",
  canonicalSha256: "1".repeat(64),
  status: "pending",
  decision: null,
  proposition: "A bounded proposition.",
  claimKind: "fact",
  confidenceExtraction: 0.9,
  expression: {
    role: "paraphrase",
    quote: "A selected factual passage.",
    selector: {
      kind: "text_quote",
      exact: "A selected factual passage.",
      prefix: "",
      suffix: "",
      start: 0,
      end: 27,
    },
  },
  existingClaimMatch: null,
  evidence: [],
  sourceCoverage: [],
  limitations: [],
};

const pendingAnalysisRun: TruthAnalysisRun = {
  ...analysisRun,
  status: "completed",
  candidates: [analysisCandidate],
  finishedAt: "2026-08-09T12:00:05Z",
};

const analysisCapabilities: TruthAnalysisCapabilities = {
  schema: "wb.cowork.truth-analysis-capabilities/v1",
  requiredCostControl: {
    enforcementClass: "hard_ceiling",
    scope: "worker_model_session",
    maximumUsdPerModelSession: 2,
  },
  researchCostControl: {
    enforcementClass: "unavailable",
    scope: "web_search_and_fetch",
    ceilingUsd: null,
    basis: "research_provider_cost_not_enforced",
  },
  providers: [{
    providerId: "claude-code",
    analysisAvailable: true,
    unavailableReason: null,
    appliesToAllModels: true,
    costControl: {
      enforcementClass: "hard_ceiling",
      ceilingUsdPerWorkerSession: 2,
      basis: "claude_code_max_budget_usd",
    },
  }],
};

const setupAnalysis = () => {
  const provider: TruthAnalysisProvider = {
    loadCapabilities: vi.fn(async () => analysisCapabilities),
    loadCurrent: vi.fn(async () => null),
    loadRun: vi.fn(async () => analysisRun),
    start: vi.fn(async () => analysisRun),
    decideCandidate: vi.fn(async () => ({
      ok: true,
      analysisRunId: analysisRun.analysisRunId,
      candidateId: "candidate-1",
      candidateStatus: "saved" as const,
      claimId: "claim-1",
      expressionId: "expression-1",
    })),
    subscribe: vi.fn(() => () => undefined),
  };
  const execution: ChatExecutionControl = {
    snapshot: {
      selection: {
        providerId: "claude-code",
        modelId: "sonnet",
        providerLabel: "Claude Code",
        modelLabel: "Sonnet",
        revision: "execution:1",
      },
      providers: [{
        id: "claude-code",
        label: "Claude Code",
        available: true,
        models: [{ id: "sonnet", label: "Sonnet", available: true }],
      }],
    },
    status: "ready",
    selecting: false,
    error: null,
    announcement: null,
    currentAvailable: true,
    select: vi.fn(async () => undefined),
    retry: vi.fn(),
  };
  return { provider, execution };
};

const openManualAction = async (
  name: "Add claim manually" | "Connect selection manually",
): Promise<void> => {
  await userEvent.click(screen.getByRole("button", { name: "Add or connect claim" }));
  await userEvent.click(await screen.findByRole("menuitem", { name }));
};

describe("TruthPanel", () => {
  it("styles only explicitly primary Truth actions as primary", () => {
    expect(truthStyles).not.toMatch(
      /\.wb-cowork-truth__actions\s+button:first-child/u,
    );
    expect(truthStyles).toMatch(/\.wb-cowork-truth\s+\.is-primary\s*\{/u);
  });

  it("does not start a second analysis before durable history restores", async () => {
    const { provider, execution } = setupAnalysis();
    let resolveCurrent: (run: TruthAnalysisRun | null) => void = () => undefined;
    const current = new Promise<TruthAnalysisRun | null>((resolve) => {
      resolveCurrent = resolve;
    });
    vi.mocked(provider.loadCurrent).mockReturnValue(current);
    const { editor } = setupEditor();
    const captureAnalysisTarget = vi.fn(async () => capturedAnalysisTarget);

    render(
      <TruthPanel
        provider={setupProvider()}
        storeId="store-1"
        documentId="doc-1"
        store={new TruthStore()}
        editor={{ ...editor, captureAnalysisTarget }}
        analysis={{ provider, execution }}
      />,
    );

    await screen.findByText(summary.proposition);
    const analyze = screen.getByRole("button", { name: "Analyze passage" });
    expect(analyze).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(analyze);
    expect(captureAnalysisTarget).not.toHaveBeenCalled();
    expect(provider.start).not.toHaveBeenCalled();

    resolveCurrent(analysisRun);
    expect(await screen.findByRole("button", { name: "Analyzing…" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(provider.start).not.toHaveBeenCalled();
  });

  it("keeps unavailable Analyze help keyboard reachable", async () => {
    const { provider, execution } = setupAnalysis();
    const { editor } = setupEditor();
    render(
      <DashboardHelpProvider enabled>
        <TruthPanel
          provider={setupProvider()}
          storeId="store-1"
          documentId="doc-1"
          store={new TruthStore()}
          editor={editor}
          analysis={{
            provider,
            execution: { ...execution, currentAvailable: false },
          }}
        />
      </DashboardHelpProvider>,
    );

    const analyze = await screen.findByRole("button", { name: "Analyze passage" });
    expect(analyze).toHaveAttribute("aria-disabled", "true");
    expect(analyze).toHaveAttribute("data-help-target", "true");
    expect(analyze).not.toBeDisabled();
    analyze.focus();
    expect(analyze).toHaveFocus();
  });

  it("captures one exact selected passage and starts analysis with the shared model", async () => {
    const railProvider = setupProvider();
    const { provider, execution } = setupAnalysis();
    const { editor } = setupEditor();
    const captureAnalysisTarget = vi.fn(async () => capturedAnalysisTarget);
    const analysisEditor = { ...editor, captureAnalysisTarget };
    render(
      <DashboardHelpProvider enabled>
        <TruthPanel
          provider={railProvider}
          storeId="store-1"
          documentId="doc-1"
          store={new TruthStore()}
          editor={analysisEditor}
          analysis={{ provider, execution }}
        />
      </DashboardHelpProvider>,
    );

    const analyze = await screen.findByRole("button", { name: "Analyze passage" });
    await waitFor(() => expect(analyze).not.toHaveAttribute("aria-disabled"));
    await userEvent.hover(analyze);
    expect(
      await screen.findByText(/sent to Claude Code · Sonnet/u),
    ).toBeVisible();
    expect(
      screen.getByText(/\$2\.00 limit is enforced on the selected account model/u),
    ).toBeVisible();
    expect(
      screen.getByText(/Web research may incur separate provider charges that Co-work cannot cap yet/u),
    ).toBeVisible();
    await userEvent.keyboard("{Escape}");
    await userEvent.click(analyze);

    await waitFor(() => expect(provider.start).toHaveBeenCalledOnce());
    expect(captureAnalysisTarget).toHaveBeenCalledOnce();
    expect(captureAnalysisTarget).toHaveBeenCalledWith("current_selection");
    expect(provider.start).toHaveBeenCalledWith({
      targetChoice: "current_selection",
      capture: capturedAnalysisTarget,
      execution: {
        providerId: "claude-code",
        modelId: "sonnet",
        providerLabel: "Claude Code",
        modelLabel: "Sonnet",
      },
    });
  });

  it("blocks a provider without a server-attested hard ceiling", async () => {
    const railProvider = setupProvider();
    const { provider, execution } = setupAnalysis();
    vi.mocked(provider.loadCapabilities).mockResolvedValue({
      ...analysisCapabilities,
      providers: [{
        providerId: "codex",
        analysisAvailable: false,
        unavailableReason:
          "Truth analysis requires a provider-enforced hard spending ceiling.",
        appliesToAllModels: true,
        costControl: {
          enforcementClass: "unavailable",
          ceilingUsdPerWorkerSession: null,
          basis: "codex_worker_has_no_budget_enforcement",
        },
      }],
    });
    const codexExecution: ChatExecutionControl = {
      ...execution,
      snapshot: {
        selection: {
          providerId: "codex",
          modelId: "gpt-5",
          providerLabel: "Codex",
          modelLabel: "GPT-5",
          revision: "execution:2",
        },
        providers: [{
          id: "codex",
          label: "Codex",
          available: true,
          models: [{ id: "gpt-5", label: "GPT-5", available: true }],
        }],
      },
    };
    const { editor } = setupEditor();
    const captureAnalysisTarget = vi.fn(async () => capturedAnalysisTarget);
    render(
      <DashboardHelpProvider enabled>
        <TruthPanel
          provider={railProvider}
          storeId="store-1"
          documentId="doc-1"
          store={new TruthStore()}
          editor={{ ...editor, captureAnalysisTarget }}
          analysis={{ provider, execution: codexExecution }}
        />
      </DashboardHelpProvider>,
    );

    const analyze = await screen.findByRole("button", { name: "Analyze passage" });
    await waitFor(() => expect(analyze).toHaveAttribute("aria-disabled", "true"));
    await userEvent.hover(analyze);
    expect(
      await screen.findByText(
        /Truth analysis requires a provider-enforced hard spending ceiling/u,
      ),
    ).toBeVisible();
    expect(screen.queryByText(/\$2\.00/u)).not.toBeInTheDocument();
    await userEvent.click(analyze);
    expect(captureAnalysisTarget).not.toHaveBeenCalled();
    expect(provider.start).not.toHaveBeenCalled();
  });

  it("serializes rapid Analyze activation into one capture and one run", async () => {
    const railProvider = setupProvider();
    const { provider, execution } = setupAnalysis();
    let resolveCapture: (capture: CoworkCapturedActionSnapshot) => void = () => undefined;
    const capture = new Promise<CoworkCapturedActionSnapshot>((resolve) => {
      resolveCapture = resolve;
    });
    const { editor } = setupEditor();
    const captureAnalysisTarget = vi.fn(() => capture);
    render(
      <TruthPanel
        provider={railProvider}
        storeId="store-1"
        documentId="doc-1"
        store={new TruthStore()}
        editor={{ ...editor, captureAnalysisTarget }}
        analysis={{ provider, execution }}
      />,
    );

    const analyze = await screen.findByRole("button", { name: "Analyze passage" });
    await waitFor(() => expect(analyze).not.toHaveAttribute("aria-disabled"));
    fireEvent.click(analyze);
    fireEvent.click(analyze);
    await waitFor(() => expect(captureAnalysisTarget).toHaveBeenCalledOnce());
    expect(provider.start).not.toHaveBeenCalled();

    resolveCapture(capturedAnalysisTarget);
    await waitFor(() => expect(provider.start).toHaveBeenCalledOnce());
  });

  it("makes a terminal timed-out run rerunnable without a restart control", async () => {
    const railProvider = setupProvider();
    const { provider, execution } = setupAnalysis();
    const timedOutRun: TruthAnalysisRun = {
      ...analysisRun,
      status: "failed",
      error: "The account-model session timed out. Analyze the passage again.",
      finishedAt: "2026-08-09T12:02:00Z",
      sourceCoverage: [{
        source: "selected_passage",
        status: "supplied",
        detail: "The exact passage capture was supplied to the run.",
        externalEgress: false,
      }],
    };
    vi.mocked(provider.loadCurrent)
      .mockResolvedValueOnce(analysisRun)
      .mockResolvedValueOnce(timedOutRun);
    let invalidate = (): void => undefined;
    provider.subscribe = vi.fn((listener) => {
      invalidate = listener;
      return () => undefined;
    });
    const { editor } = setupEditor();
    const captureAnalysisTarget = vi.fn(async () => capturedAnalysisTarget);
    render(
      <TruthPanel
        provider={railProvider}
        storeId="store-1"
        documentId="doc-1"
        store={new TruthStore()}
        editor={{ ...editor, captureAnalysisTarget }}
        analysis={{ provider, execution }}
      />,
    );

    expect(await screen.findByRole("button", { name: "Analyzing…" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    act(() => invalidate());
    expect(
      await screen.findByText(
        "The account-model session timed out. Analyze the passage again.",
      ),
    ).toBeVisible();
    const analyze = screen.getByRole("button", { name: "Analyze passage" });
    await waitFor(() => expect(analyze).not.toHaveAttribute("aria-disabled"));
    expect(screen.queryByRole("button", { name: /restart/u })).not.toBeInTheDocument();
    await userEvent.click(analyze);
    await waitFor(() => expect(provider.start).toHaveBeenCalledOnce());
  });

  it("keeps a completed run with pending claims reachable before another analysis", async () => {
    const railProvider = setupProvider();
    const { provider, execution } = setupAnalysis();
    vi.mocked(provider.loadCurrent).mockResolvedValue(pendingAnalysisRun);
    const { editor } = setupEditor();
    const captureAnalysisTarget = vi.fn(async () => capturedAnalysisTarget);
    render(
      <TruthPanel
        provider={railProvider}
        storeId="store-1"
        documentId="doc-1"
        store={new TruthStore()}
        editor={{ ...editor, captureAnalysisTarget }}
        analysis={{ provider, execution }}
      />,
    );

    await screen.findByText("1 claim ready to review.");
    const analyze = screen.getByRole("button", { name: "Analyze passage" });
    expect(analyze).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(analyze);
    expect(captureAnalysisTarget).not.toHaveBeenCalled();
    expect(provider.start).not.toHaveBeenCalled();
  });

  it.each([
    ["a completed empty run", { ...analysisRun, status: "completed", finishedAt: "2026-08-09T12:00:05Z" }],
    [
      "a fully reviewed run",
      {
        ...pendingAnalysisRun,
        candidates: [{ ...analysisCandidate, status: "saved", decision: "save_as_proposed" }],
      },
    ],
    [
      "a failed run",
      {
        ...analysisRun,
        status: "failed",
        error: "Analysis failed safely.",
        finishedAt: "2026-08-09T12:00:05Z",
      },
    ],
  ] satisfies readonly (readonly [string, TruthAnalysisRun])[])(
    "allows another analysis after %s",
    async (_label, restoredRun) => {
      const railProvider = setupProvider();
      const { provider, execution } = setupAnalysis();
      vi.mocked(provider.loadCurrent).mockResolvedValue(restoredRun);
      const { editor } = setupEditor();
      render(
        <TruthPanel
          provider={railProvider}
          storeId="store-1"
          documentId="doc-1"
          store={new TruthStore()}
          editor={{ ...editor, captureAnalysisTarget: vi.fn(async () => capturedAnalysisTarget) }}
          analysis={{ provider, execution }}
        />,
      );

      const analyze = await screen.findByRole("button", { name: "Analyze passage" });
      await waitFor(() => expect(analyze).not.toHaveAttribute("aria-disabled"));
    },
  );

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

  it("separates AI preparation from the human who added a reviewed claim", async () => {
    const provenance = {
      preparedBy: {
        kind: "agent_run" as const,
        surface: "cowork_truth_analysis" as const,
        analysisRunId: "run-1",
        candidateId: "candidate-1",
        providerId: "claude-code",
        modelId: "sonnet",
      },
      addedBy: {
        kind: "human",
        ref: "owner",
        at: "2026-08-09T12:05:00Z",
      },
    };
    const preparedConnection = { ...connection, provenance };
    const preparedSummary = {
      ...summary,
      connections: [preparedConnection],
      provenance,
    };
    render(
      <TruthPanel
        provider={setupProvider(
          [preparedSummary],
          { ...detail, ...preparedSummary },
        )}
        storeId="store-1"
        documentId="doc-1"
        store={new TruthStore()}
      />,
    );

    await userEvent.click(
      await screen.findByRole("button", { name: summary.proposition }),
    );
    expect(screen.getByText(/Prepared by an analysis/u)).toBeVisible();
    expect(screen.getByText(/Added .* by a person/u)).toBeVisible();
    expect(screen.getByText("Added by")).not.toBeVisible();
    expect(screen.getAllByText(/claude-code/u)).toHaveLength(3);
    expect(screen.queryByText("Created by")).not.toBeInTheDocument();
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

    await openManualAction("Add claim manually");
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

    await openManualAction("Add claim manually");

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

    await openManualAction("Connect selection manually");
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

    await openManualAction("Connect selection manually");
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

  it("does not offer a paid analysis mutation for a retired document", async () => {
    const railProvider = setupProvider([]);
    vi.mocked(railProvider.load).mockImplementation(async (query) => ({
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
    const { provider, execution } = setupAnalysis();
    const { editor } = setupEditor();
    const captureAnalysisTarget = vi.fn(async () => capturedAnalysisTarget);

    render(
      <TruthPanel
        provider={railProvider}
        storeId="store-1"
        documentId="doc-1"
        store={new TruthStore()}
        editor={{ ...editor, captureAnalysisTarget }}
        analysis={{ provider, execution }}
      />,
    );

    const analyze = await screen.findByRole("button", { name: "Analyze passage" });
    await waitFor(() => expect(provider.loadCurrent).toHaveBeenCalled());
    expect(analyze).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(analyze);
    expect(captureAnalysisTarget).not.toHaveBeenCalled();
    expect(provider.start).not.toHaveBeenCalled();
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

  it("scrolls on card activation only, never when an external selection or refresh restores the claim", async () => {
    const store = new TruthStore();
    const revealClaimIfOutsideViewport = vi.fn();
    const { editor } = setupEditor();
    const provider = setupProvider();
    render(<TruthPanel provider={provider} storeId="store-1" documentId="doc-1" store={store} editor={{ ...editor, revealClaimIfOutsideViewport }} />);
    await userEvent.click(await screen.findByRole("button", { name: summary.proposition }));
    await screen.findByRole("region", { name: "Claim details" });
    expect(revealClaimIfOutsideViewport).toHaveBeenCalledExactlyOnceWith(summary.claimId);
    act(() => store.selectClaim(null));
    act(() => store.selectClaim(summary.claimId));
    await screen.findByRole("region", { name: "Claim details" });
    expect(revealClaimIfOutsideViewport).toHaveBeenCalledTimes(1);
  });

  it("opens the same two-stage decision from a captured document action", async () => {
    const store = new TruthStore();
    const provider = setupProvider();
    render(<TruthPanel provider={provider} storeId="store-1" documentId="doc-1" store={store} />);
    await screen.findByRole("button", { name: summary.proposition });
    act(() => store.requestDecision(summary.claimId, "confirm"));
    await screen.findByText("Confirm this exact claim?");
    expect(provider.decideClaim).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Confirm claim" }));
    expect(provider.decideClaim).toHaveBeenCalledWith(expect.objectContaining({
      action: "confirm", expectedCanonicalSha256: "payload-hash", expectedContextSha256: "context-hash", gestureKind: "confirm",
    }));
  });

  it("requires a fresh decision stage after receipts or their bound context change", async () => {
    const onDecide = vi.fn();
    const request = { requestId: 1, claimId: detail.claimId, action: "confirm" as const };
    const rendered = render(<TruthClaimDetails claim={detail} requestedDecision={request} onClose={vi.fn()} onDecide={onDecide} />);
    expect(await screen.findByText("Confirm this exact claim?")).toBeVisible();
    const refreshed = { ...detail,
      receipts: [{ ...detail.receipts[0], quote: "Updated support" }],
      decisionBinding: { ...detail.decisionBinding!, contextSha256: "updated-context" },
    };
    rendered.rerender(<TruthClaimDetails claim={refreshed} requestedDecision={request} onClose={vi.fn()} onDecide={onDecide} />);
    await waitFor(() => expect(screen.queryByText("Confirm this exact claim?")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Confirm" })).toBeVisible();
    expect(screen.getByText("Updated support")).toBeVisible();
    expect(onDecide).not.toHaveBeenCalled();
  });

  it("uses the menu's frozen capture through the same manual controller as the header", async () => {
    const store = new TruthStore();
    const provider = setupProvider();
    const { editor, captureSelection } = setupEditor();
    render(<TruthPanel provider={provider} storeId="store-1" documentId="doc-1" store={store} editor={editor} />);
    await screen.findByRole("button", { name: summary.proposition });
    expect(store.getActions()?.manualBlockedReason).toBeNull();
    const frozen = { ...capturedSelection, captureId: "menu-capture" };
    act(() => store.getActions()?.openComposer("propose", frozen));
    await screen.findByRole("textbox", { name: "Claim" });
    expect(captureSelection).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Propose and connect" }));
    expect(provider.proposeClaim).toHaveBeenCalledWith(expect.objectContaining({ capture: frozen }));
  });

  it("starts analysis with the document menu's frozen target without recapturing the editor", async () => {
    const store = new TruthStore();
    const { provider, execution } = setupAnalysis();
    const { editor } = setupEditor();
    const captureAnalysisTarget = vi.fn(async () => capturedAnalysisTarget);
    render(<TruthPanel provider={setupProvider()} storeId="store-1" documentId="doc-1" store={store} editor={{ ...editor, captureAnalysisTarget }} analysis={{ provider, execution }} />);
    await waitFor(() => expect(store.getActions()?.analyzeBlockedReason).toBeNull());
    const frozen = { ...capturedAnalysisTarget, captureId: "menu-analysis-capture" };
    act(() => store.getActions()?.analyzePassage(frozen));
    await waitFor(() => expect(provider.start).toHaveBeenCalledWith(expect.objectContaining({ capture: frozen })));
    expect(captureAnalysisTarget).not.toHaveBeenCalled();
  });

  it("prefills a corrected proposition, kind, role, and freshly captured rejected passage", async () => {
    const rejected = { ...detail, baseStatus: "rejected" as const, availableActions: ["redact" as const], claimKind: "decision", connections: [{ ...connection, role: "paraphrase" as const }] };
    const provider = setupProvider([rejected], rejected);
    const { editor, captureSelection } = setupEditor();
    const capturePassage = vi.fn(async () => capturedSelection);
    render(<TruthPanel provider={provider} storeId="store-1" documentId="doc-1" store={new TruthStore()} editor={{ ...editor, capturePassage }} />);
    await userEvent.click(await screen.findByRole("button", { name: summary.proposition }));
    await userEvent.click(await screen.findByRole("button", { name: "Add a corrected claim" }));
    expect(await screen.findByRole("textbox", { name: "Claim" })).toHaveValue(summary.proposition);
    expect(screen.getByRole("combobox", { name: "Kind" })).toHaveValue("decision");
    expect(screen.getByRole("combobox", { name: "How the passage expresses the claim" })).toHaveValue("paraphrase");
    expect(capturePassage).toHaveBeenCalledWith(rejected.connections[0]);
    expect(captureSelection).not.toHaveBeenCalled();
  });

  it("completes rejection and re-adds the edited correction as a separate proposed claim", async () => {
    const provider = new InMemoryTruthProvider({ storeId: "store-1", documentId: "doc-1", claims: [] });
    const original = await provider.proposeClaim({ capture: capturedSelection, proposition: summary.proposition, claimKind: "fact", role: "quote" });
    const { editor } = setupEditor();
    render(<TruthPanel provider={provider} storeId="store-1" documentId="doc-1" store={new TruthStore()} editor={{ ...editor, capturePassage: async () => capturedSelection }} />);
    await userEvent.click(await screen.findByRole("button", { name: summary.proposition }));
    await userEvent.click(await screen.findByRole("button", { name: "Reject" }));
    expect((await provider.loadClaim(original.claimId!)).baseStatus).toBe("proposed");
    expect(screen.getByText(/It was not a confirmed fact/u)).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Reject claim" }));
    await userEvent.click(await screen.findByRole("button", { name: "Add a corrected claim" }));
    const claimInput = await screen.findByRole("textbox", { name: "Claim" });
    await userEvent.clear(claimInput);
    await userEvent.type(claimInput, "The document has two headings.");
    await userEvent.click(screen.getByRole("button", { name: "Propose and connect" }));
    await screen.findByRole("heading", { name: "The document has two headings." });
    const claims = (await provider.load({ scope: "document", filter: "all" })).claims;
    expect(claims.map((claim) => claim.baseStatus)).toEqual(["rejected", "proposed"]);
    expect(claims.every((claim) => !claim.isFact && claim.evidenceCount === 0)).toBe(true);
    expect(claims[1].claimId).not.toBe(original.claimId);
  });

  it("orders passages, cycles Next passage, and keeps history behind a disclosure after decisions", async () => {
    const later = { ...connection, expressionId: "later", quote: "Later passage", selector: { ...connection.selector, exact: "Later passage", start: 50 } };
    const multi = { ...detail, connections: [later, connection], connectionCount: 2 };
    const { editor, revealPassage } = setupEditor();
    render(<TruthPanel provider={setupProvider([multi], multi)} storeId="store-1" documentId="doc-1" store={new TruthStore()} editor={editor} />);
    await userEvent.click(await screen.findByRole("button", { name: summary.proposition }));
    const next = await screen.findByRole("button", { name: "Next passage" });
    await userEvent.click(next);
    await userEvent.click(next);
    await userEvent.click(next);
    expect(revealPassage.mock.calls.map(([item]) => item.expressionId)).toEqual(["expression-1", "later", "expression-1"]);
    const history = screen.getByText("History and record details").closest("details");
    expect(history).not.toHaveAttribute("open");
    expect(screen.getByRole("button", { name: "Confirm" }).compareDocumentPosition(history!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.getByText("context-hash")).not.toBeVisible();
  });

  it("uses current editor positions for offset-free passage order and follows the last passage after reordering", async () => {
    const passage = (expressionId: string, quote: string) => ({
      ...connection, expressionId, quote,
      selector: { kind: "text_quote" as const, exact: quote, prefix: "", suffix: "" },
    });
    const first = passage("first", "First passage");
    const later = passage("later", "Later passage");
    const missing = passage("missing", "Unresolved passage");
    const foreign = { ...passage("foreign", "Foreign passage"), documentId: "doc-other", currentDocument: false };
    const claim = { ...detail, connections: [foreign, missing, later, first], connectionCount: 4 };
    const provider = setupProvider([claim], claim);
    const store = new TruthStore({ selectedClaimId: claim.claimId });
    const { editor, revealPassage } = setupEditor();
    const orderPassages = vi.fn(() => [first, later, missing, foreign]);
    const rendered = render(<TruthPanel provider={provider} storeId="store-1" documentId="doc-1" store={store} editor={{ ...editor, orderPassages }} />);
    const details = await screen.findByRole("region", { name: "Claim details" });
    expect([...details.querySelectorAll(".wb-cowork-truth__connection-list q")].map((element) => element.textContent)).toEqual(["First passage", "Later passage", "Unresolved passage", "Foreign passage"]);
    expect(orderPassages).toHaveBeenCalledWith(claim.connections);
    expect(revealPassage).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Next passage" }));
    expect(revealPassage).toHaveBeenLastCalledWith(first);
    rendered.rerender(<TruthPanel provider={provider} storeId="store-1" documentId="doc-1" store={store} editor={{ ...editor, orderPassages: () => [later, first, missing, foreign] }} />);
    expect(revealPassage).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Next passage" }));
    expect(revealPassage).toHaveBeenLastCalledWith(missing);
  });

  it("keeps fallback order deterministic with current passages first and missing offsets last within each document", () => {
    const missing = { ...connection, expressionId: "z-missing", selector: { ...connection.selector, start: undefined } };
    const missingEarlierId = { ...missing, expressionId: "a-missing" };
    const foreign = { ...connection, expressionId: "foreign", documentId: "doc-other", currentDocument: false };
    expect(orderTruthPassages([foreign, missing, missingEarlierId, connection]).map((item) => item.expressionId)).toEqual(["expression-1", "a-missing", "z-missing", "foreign"]);
  });

  it("shows the Analyze passage reason without requiring Hover Help", async () => {
    const { provider, execution } = setupAnalysis();
    const { editor } = setupEditor();
    render(<TruthPanel provider={setupProvider()} storeId="store-1" documentId="doc-1" store={new TruthStore()} editor={editor} analysis={{ provider, execution }} />);
    expect(await screen.findByText("The editor cannot capture a passage right now.")).toBeVisible();
  });

  it("explains list-only analysis and creation without directing the reader away from a claim decision", async () => {
    const { provider: analysisProvider, execution } = setupAnalysis();
    const provider = setupProvider();
    const { editor } = setupEditor();
    const store = new TruthStore({ selectedClaimId: summary.claimId });
    render(<TruthPanel provider={provider} storeId="store-1" documentId="doc-1" store={store} editor={{ ...editor, captureAnalysisTarget: async () => capturedAnalysisTarget }} analysis={{ provider: analysisProvider, execution }} />);
    await screen.findByRole("region", { name: "Claim details" });
    const reason = "Analysis and claim creation are available from the claims list.";
    expect(screen.getByText(reason)).toBeVisible();
    expect(store.getActions()).toMatchObject({ analyzeBlockedReason: reason, manualBlockedReason: reason });
    act(() => {
      store.getActions()?.analyzePassage(capturedAnalysisTarget);
      store.getActions()?.openComposer("propose", capturedSelection);
    });
    expect(analysisProvider.start).not.toHaveBeenCalled();
    expect(provider.proposeClaim).not.toHaveBeenCalled();
    expect(store.getState()).toMatchObject({ selectedClaimId: summary.claimId, composer: null });
  });

  it("explains document-version staleness without claiming the exact passage disappeared or disabling navigation", async () => {
    const staleConnection = { ...connection, stale: "span_missing" as const };
    const stale = { ...detail, connections: [staleConnection] };
    const { editor, revealPassage } = setupEditor();
    render(<TruthPanel provider={setupProvider([stale], stale)} storeId="store-1" documentId="doc-1" store={new TruthStore({ selectedClaimId: stale.claimId })} editor={editor} />);
    expect(await screen.findByText("This passage no longer matches the document version it was connected to. Review its location.")).toBeVisible();
    const show = screen.getByRole("button", { name: "Show in document" });
    expect(show).toBeEnabled();
    await userEvent.click(show);
    expect(revealPassage).toHaveBeenCalledWith(staleConnection);
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
