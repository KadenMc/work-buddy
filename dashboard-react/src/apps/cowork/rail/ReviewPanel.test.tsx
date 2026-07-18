import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import { InMemoryReviewProvider } from "./InMemoryReviewProvider";
import { ReviewPanel } from "./ReviewPanel";
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

function renderPanel() {
  const store = new RailStore();
  const provider = new InMemoryReviewProvider();
  const storage = new MemoryStorage();
  const result = render(
    <ReviewPanel
      provider={provider}
      store={store}
      documentId="demo-doc"
      storage={storage}
    />,
  );
  return { store, provider, storage, ...result };
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
    expect(screen.getByText("Staged: Accept")).toBeVisible();

    const submit = screen.getByRole("button", { name: /Submit sitting/ });
    expect(submit).toHaveTextContent("Submit sitting (1)");
    await userEvent.click(submit);

    // The accepted proposal leaves the open set and the sitting clears.
    await waitFor(() => expect(screen.queryByText(S1_TLDR)).toBeNull());
    expect(
      screen.getByRole("button", { name: /Submit sitting/ }),
    ).toBeDisabled();
  });

  it("filters the stream with the lens", async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());

    await userEvent.click(screen.getByRole("button", { name: /Flags/ }));
    // Only the flag remains, the suggestion is filtered out.
    expect(screen.queryByText(S1_TLDR)).toBeNull();
    expect(
      screen.getByText("Cite the benchmark file for this figure."),
    ).toBeVisible();
  });

  it("walks the queue with the keyboard", async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());

    await userEvent.click(screen.getByRole("button", { name: "Queue" }));
    expect(screen.getByText("Item 1")).toBeVisible();
    await userEvent.keyboard("k");
    expect(screen.getByText("Item 2")).toBeVisible();
    await userEvent.keyboard("j");
    expect(screen.getByText("Item 1")).toBeVisible();
  });

  it("has no accessibility violations in the resting review state", async () => {
    const { container } = renderPanel();
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());
    await expectNoAccessibilityViolations(container);
  });
});
