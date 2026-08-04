import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import {
  demoReviewData,
  InMemoryReviewProvider,
} from "./InMemoryReviewProvider";
import { ReviewPanel } from "./ReviewPanel";
import type { EvaluationResult } from "./contracts";
import type { ReviewAnchorController, ReviewRailProvider } from "./provider";
import { RailStore } from "./store";
import { RecoverableDecisionApplyError } from "./applyRecovery";

/** A minimal in-memory Storage so a draft does not leak across tests. */
class MemoryStorage implements Storage {
  private map = new Map<string, string>();
  get length(): number {
    return this.map.size;
  }
  clear(): void {
    this.map.clear();
  }
  getItem(key: string): string | null {
    return this.map.get(key) ?? null;
  }
  key(index: number): string | null {
    return [...this.map.keys()][index] ?? null;
  }
  removeItem(key: string): void {
    this.map.delete(key);
  }
  setItem(key: string, value: string): void {
    this.map.set(key, value);
  }
}

interface RenderPanelOptions {
  readonly store?: RailStore;
  readonly provider?: ReviewRailProvider;
  readonly reviewAnchors?: ReviewAnchorController;
  readonly onScrollContainerWillDetach?: () => void;
}

function renderPanel(options: RenderPanelOptions = {}) {
  const store = options.store ?? new RailStore();
  const provider = options.provider ?? new InMemoryReviewProvider();
  const storage = new MemoryStorage();
  const result = render(
    <ReviewPanel
      provider={provider}
      store={store}
      documentId="demo-doc"
      storage={storage}
      reviewAnchors={options.reviewAnchors}
      onScrollContainerWillDetach={options.onScrollContainerWillDetach}
    />,
  );
  return { store, provider, storage, ...result };
}

function createReviewAnchors() {
  const focusAnchor = vi.fn();
  const revealAnchor = vi.fn();
  const clearFocusedAnchor = vi.fn();
  const source: ReviewAnchorController = {
    focusAnchor,
    revealAnchor,
    clearFocusedAnchor,
  };
  return { source, focusAnchor, revealAnchor, clearFocusedAnchor };
}

const S1_TLDR = "Add the vault content hash to the cache key.";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

const stageAccepts = (store: RailStore, proposalIds: readonly string[]) => {
  const data = demoReviewData();
  for (const proposalId of proposalIds) {
    const proposal = data.proposals.find((item) => item.proposalId === proposalId);
    if (proposal === undefined) throw new Error(`Unknown fixture proposal ${proposalId}`);
    store.stageDecision({
      proposalId,
      verb: "confirm",
      canonicalSha256: proposal.canonicalSha256,
    });
  }
};

describe("ReviewPanel", () => {
  it("renders the drift strip and the document-ordered stream", async () => {
    renderPanel();
    await waitFor(() =>
      expect(screen.getByText("context-bundle-cache.md")).toBeVisible(),
    );
    expect(screen.getByText("In sync, no drift")).toBeVisible();
    expect(screen.getByText(S1_TLDR)).toBeVisible();
  });

  it("keeps Verify configuration and run history out of Review", async () => {
    const data = demoReviewData();
    const provider = new InMemoryReviewProvider({
      data: {
        ...data,
        evaluationRuns: [
          {
            runId: "run-1",
            status: "completed",
            purpose: "document_review",
            targetLabel: "Whole document",
            coverageLabel: "Complete exact-string scan",
            currentVersion: true,
            resultCount: 1,
            surfacedResultCount: 0,
            coordinationStatus: "completed",
            providerLabel: "Codex",
            providerId: "codex",
            modelLabel: "GPT-5.6",
            modelId: "gpt-5.6",
            createdAt: "2026-07-28T00:00:00Z",
            finishedAt: "2026-07-28T00:00:01Z",
          },
        ],
      },
    });
    renderPanel({ provider });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());

    expect(screen.queryByText("Verify setup")).not.toBeInTheDocument();
    expect(screen.queryByText("Verify runs")).not.toBeInTheDocument();
  });

  it("stages a decision, then submits the sitting", async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());

    // Select the first suggestion, then accept it.
    await userEvent.click(screen.getByText(S1_TLDR));
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    expect(screen.getByText("Decision: Accept")).toBeVisible();

    const submit = screen.getByRole("button", { name: /Apply decisions/ });
    expect(submit).toHaveTextContent("Apply decisions (1)");
    await userEvent.click(submit);

    // The accepted proposal leaves the open set and the sitting clears.
    await waitFor(() => expect(screen.queryByText(S1_TLDR)).toBeNull());
    expect(
      screen.getByRole("button", { name: /Apply decisions/ }),
    ).toBeDisabled();
  });

  it("retries the exact confirmed subset after a generic failure", async () => {
    const data = demoReviewData();
    const store = new RailStore();
    stageAccepts(store, ["s1", "s2", "s3"]);
    const submitSitting = vi
      .fn<ReviewRailProvider["submitSitting"]>()
      .mockRejectedValueOnce(
        new RecoverableDecisionApplyError("One passage is unavailable.", {
          availableProposalIds: ["s2", "s3"],
          blockers: [
            {
              proposalId: "s1",
              reason: "passage_unavailable",
              relatedProposalIds: [],
              message: "The original passage could not be found in the current document.",
            },
          ],
        }),
      )
      .mockRejectedValueOnce(new Error("The connection was interrupted."))
      .mockResolvedValueOnce({
        ok: true,
        partial: false,
        results: ["s2", "s3"].map((proposalId) => ({
          proposalId,
          verb: "confirm" as const,
          result: "applied" as const,
          baseOk: true,
          gestureId: `gesture-${proposalId}`,
          error: null,
        })),
      });
    const provider: ReviewRailProvider = {
      load: async () => data,
      subscribe: () => () => {},
      submitSitting,
    };
    renderPanel({ provider, store });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());

    await userEvent.click(
      screen.getByRole("button", { name: "Apply decisions (3)" }),
    );

    expect(await screen.findByText("1 decision needs review")).toBeVisible();
    expect(screen.getByText("Nothing was applied. The other 2 are ready.")).toBeVisible();
    expect(
      screen.getByText("The original passage could not be found in the current document."),
    ).toBeVisible();
    expect(submitSitting).toHaveBeenCalledTimes(1);
    expect(Object.keys(store.getState().decisions)).toEqual(["s1", "s2", "s3"]);

    await userEvent.click(
      screen.getByRole("button", { name: "Apply the other 2" }),
    );

    await waitFor(() => expect(submitSitting).toHaveBeenCalledTimes(2));
    expect(
      submitSitting.mock.calls[1]?.[0].proposalDecisions.map(
        (decision) => decision.proposalId,
      ),
    ).toEqual(["s2", "s3"]);
    expect(Object.keys(store.getState().decisions)).toEqual(["s1", "s2", "s3"]);

    await userEvent.click(
      await screen.findByRole("button", { name: "Try the other 2 again" }),
    );

    await waitFor(() => expect(submitSitting).toHaveBeenCalledTimes(3));
    expect(
      submitSitting.mock.calls[2]?.[0].proposalDecisions.map(
        (decision) => decision.proposalId,
      ),
    ).toEqual(["s2", "s3"]);
    expect(Object.keys(store.getState().decisions)).toEqual(["s1"]);
    expect(
      screen.getByText("2 decisions applied; 1 decision still needs review"),
    ).toBeVisible();
  });

  it("merges retained blockers when the confirmed subset needs a second recovery", async () => {
    const data = demoReviewData();
    const store = new RailStore();
    stageAccepts(store, ["s1", "s2", "s3"]);
    const submitSitting = vi
      .fn<ReviewRailProvider["submitSitting"]>()
      .mockRejectedValueOnce(
        new RecoverableDecisionApplyError("One passage is unavailable.", {
          availableProposalIds: ["s2", "s3"],
          blockers: [
            {
              proposalId: "s1",
              reason: "passage_unavailable",
              relatedProposalIds: [],
              message: "The first passage could not be found.",
            },
          ],
        }),
      )
      .mockRejectedValueOnce(
        new RecoverableDecisionApplyError("Two edits conflict.", {
          availableProposalIds: ["s3"],
          blockers: [
            {
              proposalId: "s2",
              reason: "conflicts_with_selected_edit",
              relatedProposalIds: [],
              message: "The second edit conflicts with another selected edit.",
            },
          ],
        }),
      );
    const provider: ReviewRailProvider = {
      load: async () => data,
      subscribe: () => () => {},
      submitSitting,
    };
    renderPanel({ provider, store });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());

    await userEvent.click(
      screen.getByRole("button", { name: "Apply decisions (3)" }),
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "Apply the other 2" }),
    );

    expect(await screen.findByText("2 decisions need review")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The other decision is ready.",
    );
    expect(screen.getByText("The first passage could not be found.")).toBeVisible();
    expect(
      screen.getByText("The second edit conflicts with another selected edit."),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Apply the other decision" }),
    ).toBeEnabled();
    expect(Object.keys(store.getState().decisions)).toEqual(["s1", "s2", "s3"]);
  });

  it("lets the user remove a blocked decision that is no longer in Review", async () => {
    const complete = demoReviewData();
    const data = {
      ...complete,
      proposals: complete.proposals.filter(
        (proposal) => proposal.proposalId !== "s1",
      ),
    };
    const store = new RailStore();
    stageAccepts(store, ["s1", "s2"]);
    const provider: ReviewRailProvider = {
      load: async () => data,
      subscribe: () => () => {},
      submitSitting: async () => {
        throw new RecoverableDecisionApplyError("One suggestion is gone.", {
          availableProposalIds: ["s2"],
          blockers: [
            {
              proposalId: "s1",
              reason: "proposal_unavailable",
              relatedProposalIds: [],
              message: "This suggestion is no longer available.",
            },
          ],
        });
      },
    };
    renderPanel({ provider, store });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Apply decisions (2)" })).toBeEnabled(),
    );

    await userEvent.click(
      screen.getByRole("button", { name: "Apply decisions (2)" }),
    );

    expect(await screen.findByText("Suggestion no longer shown")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Suggestion no longer shown" }),
    ).not.toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", {
        name: "Remove decision: Suggestion no longer shown",
      }),
    );

    expect(Object.keys(store.getState().decisions)).toEqual(["s2"]);
    expect(screen.queryByText("1 decision needs review")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Apply decisions (1)" }),
    ).toBeEnabled();
  });

  it("rechecks the remaining selections after one overlapping decision is removed", async () => {
    const data = demoReviewData();
    const store = new RailStore();
    stageAccepts(store, ["s1", "s2", "s3"]);
    const submitSitting = vi
      .fn<ReviewRailProvider["submitSitting"]>()
      .mockRejectedValueOnce(
        new RecoverableDecisionApplyError("Two edits overlap.", {
          availableProposalIds: ["s3"],
          blockers: [
            {
              proposalId: "s1",
              reason: "conflicts_with_selected_edit",
              relatedProposalIds: ["s2"],
              message: "This edit overlaps another selected edit.",
            },
            {
              proposalId: "s2",
              reason: "conflicts_with_selected_edit",
              relatedProposalIds: ["s1"],
              message: "This edit overlaps another selected edit.",
            },
          ],
        }),
      )
      .mockResolvedValueOnce({
        ok: true,
        partial: false,
        results: ["s2", "s3"].map((proposalId) => ({
          proposalId,
          verb: "confirm" as const,
          result: "applied" as const,
          baseOk: true,
          gestureId: `gesture-${proposalId}`,
          error: null,
        })),
      });
    const provider: ReviewRailProvider = {
      load: async () => data,
      subscribe: () => () => {},
      submitSitting,
    };
    renderPanel({ provider, store });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());

    await userEvent.click(
      screen.getByRole("button", { name: "Apply decisions (3)" }),
    );
    await userEvent.click(
      await screen.findByRole("button", {
        name: `Remove decision: ${S1_TLDR}`,
      }),
    );

    expect(screen.queryByText("2 decisions need review")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Apply decisions (2)" }),
    ).toBeEnabled();
    expect(Object.keys(store.getState().decisions)).toEqual(["s2", "s3"]);

    await userEvent.click(
      screen.getByRole("button", { name: "Apply decisions (2)" }),
    );
    await waitFor(() => expect(submitSitting).toHaveBeenCalledTimes(2));
    expect(
      submitSitting.mock.calls[1]?.[0].proposalDecisions.map(
        (decision) => decision.proposalId,
      ),
    ).toEqual(["s2", "s3"]);
    expect(Object.keys(store.getState().decisions)).toEqual([]);
  });

  it("does not mention blocked items for an initial transport error", async () => {
    const data = demoReviewData();
    const store = new RailStore();
    stageAccepts(store, ["s1"]);
    const provider: ReviewRailProvider = {
      load: async () => data,
      subscribe: () => () => {},
      submitSitting: async () => {
        throw new Error("The connection was interrupted.");
      },
    };
    renderPanel({ provider, store });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());

    await userEvent.click(
      screen.getByRole("button", { name: "Apply decisions (1)" }),
    );

    expect(
      await screen.findByText(
        "The connection was interrupted. Your decisions are still selected, so it is safe to try again.",
      ),
    ).toBeVisible();
    expect(screen.queryByText(/blocked items/u)).not.toBeInTheDocument();
  });

  it("does not clear a decision changed while its earlier value was applying", async () => {
    const data = demoReviewData();
    const store = new RailStore();
    stageAccepts(store, ["s1"]);
    const pending = deferred<Awaited<ReturnType<ReviewRailProvider["submitSitting"]>>>();
    const provider: ReviewRailProvider = {
      load: async () => data,
      subscribe: () => () => {},
      submitSitting: () => pending.promise,
    };
    renderPanel({ provider, store });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());
    await userEvent.click(screen.getByText(S1_TLDR));

    await userEvent.click(
      screen.getByRole("button", { name: "Apply decisions (1)" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Accept" })).toBeDisabled(),
    );
    expect(screen.getByRole("button", { name: "Defer" })).toBeDisabled();

    const original = data.proposals.find((proposal) => proposal.proposalId === "s1");
    if (original === undefined) throw new Error("Missing s1 fixture");
    store.stageDecision({
      proposalId: "s1",
      verb: "defer",
      canonicalSha256: original.canonicalSha256,
    });
    pending.resolve({
      ok: true,
      partial: false,
      results: [
        {
          proposalId: "s1",
          verb: "confirm",
          result: "applied",
          baseOk: true,
          gestureId: "gesture-s1",
          error: null,
        },
      ],
    });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Apply decisions (1)" })).toBeEnabled(),
    );
    expect(store.getState().decisions.s1?.verb).toBe("defer");
  });

  it("does not surface recovery against a selection changed mid-request", async () => {
    const data = demoReviewData();
    const store = new RailStore();
    stageAccepts(store, ["s1"]);
    const pending = deferred<Awaited<ReturnType<ReviewRailProvider["submitSitting"]>>>();
    const provider: ReviewRailProvider = {
      load: async () => data,
      subscribe: () => () => {},
      submitSitting: () => pending.promise,
    };
    renderPanel({ provider, store });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());
    await userEvent.click(
      screen.getByRole("button", { name: "Apply decisions (1)" }),
    );
    stageAccepts(store, ["s2"]);
    pending.reject(
      new RecoverableDecisionApplyError("The first passage moved.", {
        availableProposalIds: [],
        blockers: [
          {
            proposalId: "s1",
            reason: "passage_unavailable",
            relatedProposalIds: [],
            message: "The first passage moved.",
          },
        ],
      }),
    );

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Apply decisions (2)" })).toBeEnabled(),
    );
    expect(screen.queryByText("1 decision needs review")).not.toBeInTheDocument();
  });

  it("explains a provider partial result instead of silently clearing its safe items", async () => {
    const data = demoReviewData();
    const store = new RailStore();
    stageAccepts(store, ["s1", "s2"]);
    const provider: ReviewRailProvider = {
      load: async () => data,
      subscribe: () => () => {},
      submitSitting: async () => ({
        ok: true,
        partial: true,
        results: [
          {
            proposalId: "s1",
            verb: "confirm",
            result: "applied",
            baseOk: true,
            gestureId: "gesture-s1",
            error: null,
          },
          {
            proposalId: "s2",
            verb: "confirm",
            result: "rejected_stale_view",
            baseOk: false,
            gestureId: null,
            error: "This suggestion changed since it was reviewed.",
          },
        ],
      }),
    };
    renderPanel({ provider, store });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());

    await userEvent.click(
      screen.getByRole("button", { name: "Apply decisions (2)" }),
    );

    expect(
      await screen.findByText("1 decision applied; 1 decision still needs review"),
    ).toBeVisible();
    expect(screen.getByText("The blocked decisions remain selected.")).toBeVisible();
    expect(Object.keys(store.getState().decisions)).toEqual(["s2"]);
  });

  it("filters the stream with the lens", async () => {
    const anchors = createReviewAnchors();
    const onScrollContainerWillDetach = vi.fn();
    const { store } = renderPanel({
      reviewAnchors: anchors.source,
      onScrollContainerWillDetach,
    });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());

    await userEvent.click(screen.getByText(S1_TLDR));
    await waitFor(() =>
      expect(anchors.revealAnchor).toHaveBeenCalledWith("s1", "proposal"),
    );
    expect(screen.getByRole("button", { name: "Accept" })).toBeVisible();
    anchors.clearFocusedAnchor.mockClear();

    await userEvent.click(screen.getByRole("button", { name: /Flags/ }));
    expect(onScrollContainerWillDetach).toHaveBeenCalledOnce();
    // Only the flag remains, the suggestion is filtered out.
    expect(screen.queryByText(S1_TLDR)).toBeNull();
    expect(
      screen.getByText("Cite the benchmark file for this figure."),
    ).toBeVisible();
    // A card lens clears stale focus, but never asks the editor to remove marks.
    await waitFor(() => expect(store.getState().selectedId).toBeNull());
    expect(store.getState().selectedKind).toBeNull();
    expect(anchors.clearFocusedAnchor).toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.getByText("Select an item to decide on it.")).toBeVisible();
  });

  it("detaches the canonical Review scroll binding before entering Queue", async () => {
    const onScrollContainerWillDetach = vi.fn();
    renderPanel({ onScrollContainerWillDetach });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());

    await userEvent.click(screen.getByRole("button", { name: "Queue" }));

    expect(onScrollContainerWillDetach).toHaveBeenCalledOnce();
  });

  it("keeps a preselected item passive instead of replaying navigation", async () => {
    const anchors = createReviewAnchors();
    const store = new RailStore({
      selectedId: "s1",
      selectedKind: "proposal",
    });
    renderPanel({ reviewAnchors: anchors.source, store });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());

    await waitFor(() =>
      expect(anchors.focusAnchor).toHaveBeenCalledWith("s1", "proposal"),
    );
    expect(anchors.revealAnchor).not.toHaveBeenCalled();
  });

  it("reveals a card activation, then flashes its explicit passage affordance", async () => {
    const anchors = createReviewAnchors();
    renderPanel({ reviewAnchors: anchors.source });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());
    anchors.revealAnchor.mockClear();

    await userEvent.click(screen.getByText(S1_TLDR));
    await waitFor(() =>
      expect(anchors.revealAnchor).toHaveBeenCalledWith("s1", "proposal"),
    );

    // Re-activating an already-selected card remains a navigation command.
    anchors.revealAnchor.mockClear();
    await userEvent.click(screen.getByText(S1_TLDR));
    expect(anchors.revealAnchor).toHaveBeenCalledWith("s1", "proposal");

    anchors.revealAnchor.mockClear();
    await userEvent.click(
      screen.getByRole("button", {
        name: "Go to paragraph 2 in the document",
      }),
    );
    expect(anchors.revealAnchor).toHaveBeenCalledWith("s1", "proposal", {
      flash: true,
    });
  });

  it("selects an unselected Stream item before revealing its passage", async () => {
    const anchors = createReviewAnchors();
    const { store } = renderPanel({ reviewAnchors: anchors.source });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());
    anchors.revealAnchor.mockClear();

    await userEvent.click(
      screen.getByRole("button", {
        name: "Go to list, item 2 in the document",
      }),
    );

    await waitFor(() => expect(store.getState().selectedId).toBe("s2"));
    expect(store.getState().selectedKind).toBe("proposal");
    expect(
      screen.getByRole("button", {
        name: "Name the exactness versus hashing-cost tradeoff.",
        pressed: true,
      }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Accept" })).toBeVisible();
    expect(anchors.revealAnchor).toHaveBeenCalledWith("s2", "proposal", {
      flash: true,
    });
  });

  it("opens a correction outside the current filter and Queue position before revealing it", async () => {
    const data = demoReviewData();
    const result: EvaluationResult = {
      resultId: "result-s2",
      runId: "run-1",
      kind: "nonconforming",
      criterionLabel: "Paragraph flow",
      criterionStatement: "Keep paragraphs readable.",
      checkLabel: "Paragraph flow check",
      methodLabel: "Deterministic scan",
      explanation: "A correction is ready.",
      quoteAnchor: {
        exact: "every collector output",
        prefix: "Keys on a digest of ",
        suffix: ".",
      },
      coverageLabel: "Whole document",
      limitations: [],
      currentVersion: true,
      disposition: "surface_proposal",
      canonicalSha256: "a".repeat(64),
      proposalIds: ["s2"],
      createdAt: "2026-08-04T12:00:00Z",
    };
    const provider = new InMemoryReviewProvider({
      data: { ...data, evaluationResults: [result] },
    });
    const store = new RailStore({ mode: "queue", filter: "flags" });
    const anchors = createReviewAnchors();
    renderPanel({ provider, store, reviewAnchors: anchors.source });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Review correction" }),
      ).toBeVisible(),
    );
    anchors.revealAnchor.mockClear();

    await userEvent.click(
      screen.getByRole("button", { name: "Review correction" }),
    );

    await waitFor(() => expect(store.getState().mode).toBe("stream"));
    expect(store.getState().filter).toBe("all");
    expect(store.getState().selectedId).toBe("s2");
    expect(store.getState().selectedKind).toBe("proposal");
    expect(anchors.revealAnchor).toHaveBeenCalledWith("s2", "proposal", {
      flash: true,
    });
  });

  it("walks the queue with the keyboard", async () => {
    const anchors = createReviewAnchors();
    const { store } = renderPanel({ reviewAnchors: anchors.source });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());

    await userEvent.click(screen.getByRole("button", { name: "Queue" }));
    expect(screen.getByText("Item 1")).toBeVisible();
    await waitFor(() =>
      expect(anchors.focusAnchor).toHaveBeenCalledWith("s1", "proposal"),
    );
    anchors.revealAnchor.mockClear();

    await userEvent.keyboard("k");
    expect(screen.getByText("Item 2")).toBeVisible();
    await waitFor(() =>
      expect(anchors.revealAnchor).toHaveBeenCalledWith("s2", "proposal"),
    );
    expect(store.getState().selectedId).toBe("s2");
    expect(store.getState().selectedKind).toBe("proposal");

    await userEvent.keyboard("j");
    expect(screen.getByText("Item 1")).toBeVisible();
  });

  it("qualifies selection by kind when proposal and claim ids collide", async () => {
    const data = demoReviewData();
    const proposal = { ...data.proposals[0], proposalId: "shared" };
    const claim = { ...data.claims[0], claimId: "shared" };
    const provider = new InMemoryReviewProvider({
      data: {
        ...data,
        proposals: [proposal],
        claims: [claim],
        expressions: [],
      },
    });
    const store = new RailStore({
      selectedId: "shared",
      selectedKind: "claim",
    });
    renderPanel({ provider, store });

    await waitFor(() =>
      expect(screen.getByText(claim.proposition)).toBeVisible(),
    );
    expect(
      screen.getByRole("button", { name: claim.proposition }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(
      screen.getByRole("button", { name: proposal.tldr }),
    ).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText(/^Claim, "/)).toBeVisible();
  });

  it("has no accessibility violations in the resting review state", async () => {
    const { container } = renderPanel();
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());
    await expectNoAccessibilityViolations(container);
  });
});
