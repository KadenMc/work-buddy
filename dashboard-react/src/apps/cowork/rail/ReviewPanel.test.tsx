import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import {
  demoReviewData,
  InMemoryReviewProvider,
} from "./InMemoryReviewProvider";
import { ReviewPanel } from "./ReviewPanel";
import type { AnchorRectSource } from "./provider";
import { RailStore } from "./store";

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
  readonly provider?: InMemoryReviewProvider;
  readonly anchorRects?: AnchorRectSource;
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
      anchorRects={options.anchorRects}
    />,
  );
  return { store, provider, storage, ...result };
}

function createAnchorRects() {
  const focusAnchor = vi.fn();
  const clearFocusedAnchor = vi.fn();
  const source: AnchorRectSource = {
    anchorRect: () => null,
    scrollToAnchor: vi.fn(),
    focusAnchor,
    clearFocusedAnchor,
    subscribe: () => () => {},
  };
  return { source, focusAnchor, clearFocusedAnchor };
}

const S1_TLDR = "Add the vault content hash to the cache key.";

describe("ReviewPanel", () => {
  it("renders the drift strip and the document-ordered stream", async () => {
    renderPanel();
    await waitFor(() =>
      expect(screen.getByText("context-bundle-cache.md")).toBeVisible(),
    );
    expect(screen.getByText("In sync, no drift")).toBeVisible();
    expect(screen.getByText(S1_TLDR)).toBeVisible();
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

  it("filters the stream with the lens", async () => {
    const anchors = createAnchorRects();
    const { store } = renderPanel({ anchorRects: anchors.source });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());

    await userEvent.click(screen.getByText(S1_TLDR));
    await waitFor(() =>
      expect(anchors.focusAnchor).toHaveBeenCalledWith("s1", "proposal", {
        scroll: true,
      }),
    );
    expect(screen.getByRole("button", { name: "Accept" })).toBeVisible();
    anchors.clearFocusedAnchor.mockClear();

    await userEvent.click(screen.getByRole("button", { name: /Flags/ }));
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

  it("scrolls and focuses a selected card, then flashes its explicit anchor affordance", async () => {
    const anchors = createAnchorRects();
    renderPanel({ anchorRects: anchors.source });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());
    anchors.focusAnchor.mockClear();

    await userEvent.click(screen.getByText(S1_TLDR));
    await waitFor(() =>
      expect(anchors.focusAnchor).toHaveBeenCalledWith("s1", "proposal", {
        scroll: true,
      }),
    );

    anchors.focusAnchor.mockClear();
    await userEvent.click(
      screen.getByRole("button", {
        name: "Go to paragraph 2 in the document",
      }),
    );
    expect(anchors.focusAnchor).toHaveBeenCalledWith("s1", "proposal", {
      scroll: true,
      flash: true,
    });
  });

  it("selects an unselected Stream item before revealing its passage", async () => {
    const anchors = createAnchorRects();
    const { store } = renderPanel({ anchorRects: anchors.source });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());
    anchors.focusAnchor.mockClear();

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
    expect(anchors.focusAnchor).toHaveBeenCalledWith("s2", "proposal", {
      scroll: true,
      flash: true,
    });
  });

  it("walks the queue with the keyboard", async () => {
    const anchors = createAnchorRects();
    const { store } = renderPanel({ anchorRects: anchors.source });
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());

    await userEvent.click(screen.getByRole("button", { name: "Queue" }));
    expect(screen.getByText("Item 1")).toBeVisible();
    await waitFor(() =>
      expect(anchors.focusAnchor).toHaveBeenCalledWith("s1", "proposal", {
        scroll: true,
      }),
    );
    anchors.focusAnchor.mockClear();

    await userEvent.keyboard("k");
    expect(screen.getByText("Item 2")).toBeVisible();
    await waitFor(() =>
      expect(anchors.focusAnchor).toHaveBeenCalledWith("s2", "proposal", {
        scroll: true,
      }),
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
