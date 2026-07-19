import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { DashboardHelpProvider } from "../../../dashboard/help";
import { expectNoAccessibilityViolations } from "../../../test/setup";
import { CoworkRail } from "./CoworkRail";
import { InMemoryReviewProvider } from "./InMemoryReviewProvider";
import { createDemoChatProvider } from "./chatFixture";

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

function renderRail(storage: Storage = new MemoryStorage()) {
  return render(
    <CoworkRail
      documentId="demo-doc"
      reviewProvider={new InMemoryReviewProvider()}
      chatProvider={createDemoChatProvider("conv-1")}
      conversationId="conv-1"
      storage={storage}
    />,
  );
}

const S1_TLDR = "Add the vault content hash to the cache key.";

describe("CoworkRail", () => {
  it("frames the Review and Chat tabs with Review active", async () => {
    renderRail();
    expect(screen.getByRole("tab", { name: "Review" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: /Chat/ })).toBeVisible();
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());
  });

  it("gives the Review and Chat tabs their own hover help in help mode", () => {
    render(
      <DashboardHelpProvider enabled>
        <CoworkRail
          documentId="demo-doc"
          reviewProvider={new InMemoryReviewProvider()}
          chatProvider={createDemoChatProvider("conv-1")}
          conversationId="conv-1"
          storage={new MemoryStorage()}
        />
      </DashboardHelpProvider>,
    );
    // Each tab is its own help target, so the two can be described separately.
    expect(screen.getByRole("tab", { name: "Review" })).toHaveAttribute(
      "data-help-target",
      "true",
    );
    expect(screen.getByRole("tab", { name: /Chat/ })).toHaveAttribute(
      "data-help-target",
      "true",
    );
  });

  it("mounts the house ChatPanel on the Chat tab and sends a message", async () => {
    renderRail();
    await userEvent.click(screen.getByRole("tab", { name: /Chat/ }));

    await waitFor(() =>
      expect(
        screen.getByText(/I proposed a few tracked edits/),
      ).toBeVisible(),
    );
    const composer = screen.getByRole("textbox", { name: "Message" });
    await userEvent.type(composer, "Why does paragraph 2 say that?");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() =>
      expect(
        screen.getByText(/turn "Why does paragraph 2 say that\?" into a tracked-change proposal/),
      ).toBeVisible(),
    );
  });

  it("retains a partly-marked sitting across a remount through the draft", async () => {
    const storage = new MemoryStorage();
    const first = renderRail(storage);
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());
    await userEvent.click(screen.getByText(S1_TLDR));
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    expect(screen.getByText("Staged: Accept")).toBeVisible();

    first.unmount();

    // A fresh rail with a fresh store, but the same storage, restores the draft.
    renderRail(storage);
    await waitFor(() => expect(screen.getByText("Staged: Accept")).toBeVisible());
  });

  it("has no accessibility violations on the Review composition", async () => {
    const { container } = renderRail();
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());
    await expectNoAccessibilityViolations(container);
  });

  it("has no accessibility violations on the Chat composition", async () => {
    const { container } = renderRail();
    await userEvent.click(screen.getByRole("tab", { name: /Chat/ }));
    await waitFor(() =>
      expect(screen.getByText(/I proposed a few tracked edits/)).toBeVisible(),
    );
    await expectNoAccessibilityViolations(container);
  });
});
