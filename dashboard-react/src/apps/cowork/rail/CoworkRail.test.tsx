import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DashboardHelpProvider } from "../../../dashboard/help";
import { expectNoAccessibilityViolations } from "../../../test/setup";
import type {
  ChatConversationProvider,
  ChatExecutionControl,
} from "../../../widget-library/chat";
import { CoworkChatAnnotations } from "../chat";
import {
  loadChatDraft,
  loadRailTab,
  saveChatDraft,
  saveRailTab,
} from "../guards";
import { CoworkRail } from "./CoworkRail";
import {
  demoReviewData,
  InMemoryReviewProvider,
} from "./InMemoryReviewProvider";
import type { ReviewRailProvider } from "./provider";
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

function renderRail(
  storage: Storage = new MemoryStorage(),
  reviewScrollRef?: (element: HTMLElement | null) => void,
  onReviewScrollWillDetach?: () => void,
) {
  return render(
    <CoworkRail
      documentId="demo-doc"
      reviewProvider={new InMemoryReviewProvider()}
      chat={{
        kind: "ready",
        provider: createDemoChatProvider("conv-1"),
        conversationId: "conv-1",
        draftStorageId: "conv-1",
        agent: {
          status: "running",
          alive: true,
          started: true,
          error: null,
        },
      }}
      storage={storage}
      reviewScrollRef={reviewScrollRef}
      onReviewScrollWillDetach={onReviewScrollWillDetach}
    />,
  );
}

const S1_TLDR = "Add the vault content hash to the cache key.";

const COTHINK_ITEM = {
  itemId: "cothink-1",
  subtype: "alternative_perspective" as const,
  content: "What if this decision should remain reversible?",
  rationale: "The current draft treats it as permanent.",
  targetLabel: "Whole document",
  quoteAnchor: null,
  status: "open" as const,
  currentVersion: true,
  canonicalSha256: "b".repeat(64),
  createdAt: "2026-07-28T00:00:00Z",
};

const chatExecution = (
  select: ChatExecutionControl["select"] = vi.fn(async () => {}),
): ChatExecutionControl => ({
  snapshot: {
    selection: {
      providerId: "claude-code",
      modelId: "sonnet",
      providerLabel: "Claude Code",
      modelLabel: "Sonnet",
      revision: "",
    },
    providers: [
      {
        id: "claude-code",
        label: "Claude Code",
        available: true,
        models: [{ id: "sonnet", label: "Sonnet", available: true }],
      },
      {
        id: "codex",
        label: "Codex",
        available: true,
        models: [{ id: "gpt-5.6", label: "GPT-5.6", available: true }],
      },
    ],
  },
  status: "ready",
  selecting: false,
  error: null,
  announcement: null,
  currentAvailable: true,
  select,
  retry: vi.fn(),
});

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

  it("restores the persisted rail tab on mount and persists a tab change", async () => {
    const storage = new MemoryStorage();
    saveRailTab(storage, "demo-doc", "chat");

    renderRail(storage);
    expect(screen.getByRole("tab", { name: /Chat/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    await userEvent.click(screen.getByRole("tab", { name: "Review" }));
    await waitFor(() => expect(loadRailTab(storage, "demo-doc")).toBe("review"));
  });

  it("attaches Review scroll persistence only while Review is visible", async () => {
    const storage = new MemoryStorage();
    saveRailTab(storage, "demo-doc", "chat");
    const reviewScrollRef = vi.fn();
    const onReviewScrollWillDetach = vi.fn();

    renderRail(storage, reviewScrollRef, onReviewScrollWillDetach);
    expect(screen.getByRole("tab", { name: /Chat/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      reviewScrollRef.mock.calls.some(([element]) => element instanceof HTMLElement),
    ).toBe(false);

    await userEvent.click(screen.getByRole("tab", { name: "Review" }));
    await waitFor(() =>
      expect(
        reviewScrollRef.mock.calls.some(
          ([element]) => element instanceof HTMLElement,
        ),
      ).toBe(true),
    );

    await userEvent.click(screen.getByRole("tab", { name: /Chat/ }));
    expect(onReviewScrollWillDetach).toHaveBeenCalledOnce();
    await waitFor(() =>
      expect(
        reviewScrollRef.mock.calls.some(([element]) => element === null),
      ).toBe(true),
    );
  });

  it("does not start an agent merely because the persisted Chat tab is restored", () => {
    const storage = new MemoryStorage();
    saveRailTab(storage, "demo-doc", "chat");
    const onChatSelected = vi.fn();
    render(
      <CoworkRail
        documentId="demo-doc"
        reviewProvider={new InMemoryReviewProvider()}
        chat={{
          kind: "idle",
          draftStorageId: "document:demo-doc",
        }}
        onChatSelected={onChatSelected}
        storage={storage}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "Preparing chat…" }),
    ).toBeVisible();
    expect(onChatSelected).not.toHaveBeenCalled();
  });

  it("starts from a current Chat-tab click, not from mounting the idle gate", async () => {
    const onChatSelected = vi.fn();
    render(
      <CoworkRail
        documentId="demo-doc"
        reviewProvider={new InMemoryReviewProvider()}
        chat={{
          kind: "idle",
          draftStorageId: "document:demo-doc",
        }}
        onChatSelected={onChatSelected}
        storage={new MemoryStorage()}
      />,
    );

    expect(onChatSelected).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("tab", { name: /Chat/ }));
    expect(onChatSelected).toHaveBeenCalledOnce();
  });

  it("does not expose model lifecycle controls in the preparation gate", async () => {
    render(
      <CoworkRail
        documentId="demo-doc"
        reviewProvider={new InMemoryReviewProvider()}
        chat={{
          kind: "idle",
          draftStorageId: "document:demo-doc",
        }}
        chatExecution={chatExecution()}
        storage={new MemoryStorage()}
      />,
    );

    await userEvent.click(screen.getByRole("tab", { name: /Chat/ }));
    expect(screen.queryByRole("button", { name: /Start chat/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /Restart chat/i })).toBeNull();
  });

  it("keeps the preparation gate passive for a read-only execution profile", () => {
    const execution = chatExecution();
    render(
      <CoworkRail
        documentId="demo-doc"
        reviewProvider={new InMemoryReviewProvider()}
        chat={{
          kind: "idle",
          draftStorageId: "document:demo-doc",
        }}
        chatExecution={{
          ...execution,
          snapshot: {
            ...execution.snapshot!,
            readOnly: true,
          },
        }}
        initialTab="chat"
        storage={new MemoryStorage()}
      />,
    );

    expect(screen.getByRole("heading", { name: "Preparing chat…" })).toBeVisible();
    expect(screen.queryByRole("button", { name: /Start chat/i })).toBeNull();
  });

  it("does not let a hidden Queue consume shortcuts from Chat", async () => {
    renderRail();
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());
    await userEvent.click(screen.getByRole("button", { name: "Queue" }));
    expect(screen.getByText("Item 1")).toBeVisible();

    await userEvent.click(screen.getByRole("tab", { name: /Chat/ }));
    await userEvent.keyboard("k");
    await userEvent.click(screen.getByRole("tab", { name: "Review" }));

    expect(screen.getByText("Item 1")).toBeVisible();
  });

  it("owns one rich-chat load and subscription instead of polling twice", async () => {
    const provider: ChatConversationProvider = {
      loadConversation: vi.fn(async () => ({
        conversationId: "opaque-rich-chat",
        status: "open" as const,
        agentLiveness: "alive" as const,
        messages: [],
      })),
      sendMessage: vi.fn(async () => ({
        conversationId: "opaque-rich-chat",
        status: "open" as const,
        agentLiveness: "alive" as const,
        messages: [],
      })),
      subscribe: vi.fn(() => () => {}),
    };
    render(
      <CoworkRail
        documentId="demo-doc"
        reviewProvider={new InMemoryReviewProvider()}
        chat={{
          kind: "ready",
          provider,
          conversationId: "opaque-rich-chat",
          draftStorageId: "document:demo-doc",
          agent: {
            status: "running",
            alive: true,
            started: false,
            error: null,
          },
        }}
        chatAnnotations={new CoworkChatAnnotations()}
        storage={new MemoryStorage()}
      />,
    );

    await waitFor(() => expect(provider.loadConversation).toHaveBeenCalledOnce());
    expect(provider.subscribe).toHaveBeenCalledOnce();
  });

  it("gives the Review and Chat tabs their own hover help in help mode", () => {
    render(
      <DashboardHelpProvider enabled>
        <CoworkRail
          documentId="demo-doc"
          reviewProvider={new InMemoryReviewProvider()}
          chat={{
            kind: "ready",
            provider: createDemoChatProvider("conv-1"),
            conversationId: "conv-1",
            draftStorageId: "conv-1",
            agent: {
              status: "running",
              alive: true,
              started: true,
              error: null,
            },
          }}
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

  it("retains the chat draft on the plain panel and persists typing", async () => {
    const storage = new MemoryStorage();
    saveChatDraft(storage, "conv-1", "a half-written question");

    renderRail(storage);
    await userEvent.click(screen.getByRole("tab", { name: /Chat/ }));
    await waitFor(() =>
      expect(
        screen.getByText(/I proposed a few tracked edits/),
      ).toBeVisible(),
    );

    const composer = screen.getByRole("textbox", { name: "Message" });
    expect(composer).toHaveValue("a half-written question");

    await userEvent.type(composer, " and more");
    await waitFor(() =>
      expect(loadChatDraft(storage, "conv-1")).toBe(
        (composer as HTMLTextAreaElement).value,
      ),
    );
    expect(loadChatDraft(storage, "conv-1")).toContain("and more");
  });

  it("retains a partly-marked sitting across a remount through the draft", async () => {
    const storage = new MemoryStorage();
    const first = renderRail(storage);
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());
    await userEvent.click(screen.getByText(S1_TLDR));
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    expect(screen.getByText("Decision: Accept")).toBeVisible();

    first.unmount();

    // A fresh rail with a fresh store, but the same storage, restores the draft.
    renderRail(storage);
    await waitFor(() => expect(screen.getByText("Decision: Accept")).toBeVisible());
  });

  it("routes an exact Co-think item into Chat before switching tabs", async () => {
    const discussCothink = vi.fn(async () => ({
      conversationId: "conv-1",
      messageId: "discussion-message-1",
    }));
    const provider = new InMemoryReviewProvider({
      data: {
        ...demoReviewData(),
        cothinkItems: [COTHINK_ITEM],
      },
    }) as InMemoryReviewProvider & ReviewRailProvider;
    provider.discussCothink = discussCothink;

    render(
      <CoworkRail
        documentId="demo-doc"
        reviewProvider={provider}
        chat={{
          kind: "ready",
          provider: createDemoChatProvider("conv-1"),
          conversationId: "conv-1",
          draftStorageId: "conv-1",
          agent: {
            status: "running",
            alive: true,
            started: true,
            error: null,
          },
        }}
        storage={new MemoryStorage()}
      />,
    );
    await waitFor(() =>
      expect(screen.getByText(COTHINK_ITEM.content)).toBeVisible(),
    );

    await userEvent.click(screen.getByRole("button", { name: "Discuss" }));

    await waitFor(() =>
      expect(discussCothink).toHaveBeenCalledWith(
        COTHINK_ITEM.itemId,
        COTHINK_ITEM.canonicalSha256,
      ),
    );
    expect(screen.getByRole("tab", { name: /Chat/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
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
