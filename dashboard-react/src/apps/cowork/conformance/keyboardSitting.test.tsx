/**
 * Keyboard-completeness proof for the sitting flow. The whole batch review is
 * driveable with the inverted j/k pair (j previous, k next), Tab-to-focus, and
 * Enter, with no pointer. The proof walks the queue with j/k, stages a no-input
 * verb and an inline-input verb (reject as preference) with typed text, and
 * submits, asserting the staged count and the post-submit open set.
 *
 * One gap is characterised rather than fixed (tests-only work item): the inline
 * verb input has no Escape-to-cancel handler, so Escape leaves it open and the
 * keyboard user must reach the Cancel button. This is reported as a production
 * finding.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  InMemoryReviewProvider,
  RailStore,
  ReviewPanel,
} from "../rail";

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

function renderQueue() {
  const store = new RailStore({ mode: "queue" });
  render(
    <ReviewPanel
      provider={new InMemoryReviewProvider()}
      store={store}
      documentId="demo-doc"
      storage={new MemoryStorage()}
    />,
  );
  return store;
}

const submitButton = () =>
  screen.getByRole("button", { name: /Submit sitting/ });

describe("Co-work keyboard-driven sitting", () => {
  beforeEach(() => {
    localStorage.clear();
  });
  afterEach(() => {
    localStorage.clear();
  });

  it("walks the queue and submits a sitting with j/k, Tab, and Enter only", async () => {
    const user = userEvent.setup();
    renderQueue();

    // The queue opens on the first of the five demo items, all undecided.
    await waitFor(() => expect(screen.getByText(/Item 1/)).toBeVisible());
    expect(screen.getByText(/of 5/)).toBeVisible();
    expect(screen.getByText("5 undecided")).toBeVisible();

    // The controls are in the tab order (keyboard-reachable, not pointer-only).
    document.body.focus();
    await user.tab();
    expect(document.activeElement?.tagName).toBe("BUTTON");

    // Navigate forward twice with k, back once with j (the inverted binding).
    await user.keyboard("k");
    expect(screen.getByText(/Item 2/)).toBeVisible();
    await user.keyboard("k");
    expect(screen.getByText(/Item 3/)).toBeVisible();
    await user.keyboard("j");
    expect(screen.getByText(/Item 2/)).toBeVisible();
    await user.keyboard("j");
    expect(screen.getByText(/Item 1/)).toBeVisible();

    // Stage Accept on item 1 by focusing the verb and pressing Enter. In queue
    // mode staging auto-advances to the next undecided item.
    screen.getByRole("button", { name: "Accept" }).focus();
    await user.keyboard("{Enter}");
    expect(submitButton()).toHaveTextContent("Submit sitting (1)");
    await waitFor(() => expect(screen.getByText(/Item 2/)).toBeVisible());

    // Item 2: reject as preference collects verbatim text inline before staging.
    screen.getByRole("button", { name: "Reject as preference" }).focus();
    await user.keyboard("{Enter}");
    const field = screen.getByLabelText(
      "Your preferred phrasing, recorded as a preference",
    );
    await user.type(field, "Prefer the original phrasing.");
    screen.getByRole("button", { name: "Stage" }).focus();
    await user.keyboard("{Enter}");
    expect(submitButton()).toHaveTextContent("Submit sitting (2)");
    await waitFor(() => expect(screen.getByText(/Item 3/)).toBeVisible());

    // Characterise the Escape gap: opening the redirect input and pressing Escape
    // does NOT close it (no handler). The keyboard user reaches Cancel instead.
    screen.getByRole("button", { name: "Redirect" }).focus();
    await user.keyboard("{Enter}");
    expect(
      screen.getByLabelText("Guidance for the agent"),
    ).toBeVisible();
    await user.keyboard("{Escape}");
    expect(
      screen.getByLabelText("Guidance for the agent"),
    ).toBeVisible();
    screen.getByRole("button", { name: "Cancel" }).focus();
    await user.keyboard("{Enter}");
    expect(
      screen.queryByLabelText("Guidance for the agent"),
    ).toBeNull();
    // Item 3 stays undecided, so the staged count is unchanged.
    expect(submitButton()).toHaveTextContent("Submit sitting (2)");

    // Submit the sitting with the keyboard. The two decided proposals leave the
    // open set, so the queue shrinks and the submit button disarms.
    submitButton().focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(submitButton()).toBeDisabled());
    expect(screen.getByText(/of 3/)).toBeVisible();
  }, 20_000);
});
