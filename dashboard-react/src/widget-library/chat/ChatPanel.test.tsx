import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../test/setup";
import { ChatPanel } from "./ChatPanel";
import type { ChatMessage } from "./contracts";

const messages: ChatMessage[] = [
  { id: "m1", author: "user", content: "Hi" },
  { id: "m2", author: "assistant", content: "Hello" },
];

describe("ChatPanel", () => {
  it("renders the title header, transcript, and composer when ready", () => {
    render(<ChatPanel title="Doc chat" messages={messages} onSend={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Doc chat" })).toBeInTheDocument();
    expect(screen.getByRole("log", { name: "Doc chat" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Message" })).toBeInTheDocument();
  });

  it("renders a custom header slot in place of the default title", () => {
    render(
      <ChatPanel
        title="Doc chat"
        header={<div>Custom header content</div>}
        messages={messages}
        onSend={vi.fn()}
      />,
    );
    expect(screen.getByText("Custom header content")).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Doc chat" }),
    ).not.toBeInTheDocument();
  });

  it("shows the loading host state", () => {
    render(<ChatPanel status="loading" messages={[]} />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading conversation");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("shows the empty host state with custom copy", () => {
    render(
      <ChatPanel status="empty" messages={[]} emptyMessage="No document chat yet." />,
    );
    expect(screen.getByText("No document chat yet.")).toBeInTheDocument();
  });

  it("shows the error host state and retries", async () => {
    const onRetry = vi.fn();
    render(
      <ChatPanel
        status="error"
        messages={[]}
        errorMessage="Could not reach the conversation."
        onRetry={onRetry}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Could not reach the conversation.",
    );
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("keeps the transcript readable but replaces the composer when read-only", () => {
    render(
      <ChatPanel
        status="read-only"
        title="Archived"
        messages={messages}
        onSend={vi.fn()}
        readOnlyReason="This conversation is closed."
      />,
    );
    expect(screen.getByRole("log", { name: "Archived" })).toBeInTheDocument();
    expect(screen.getByText("This conversation is closed.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("wires the composer send intent", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    render(<ChatPanel title="Doc chat" messages={messages} onSend={onSend} />);
    await userEvent.type(
      screen.getByRole("textbox", { name: "Message" }),
      "a reply",
    );
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledWith("a reply"));
  });

  it("seeds the composer from initialValue", () => {
    render(
      <ChatPanel
        title="Doc chat"
        messages={messages}
        onSend={vi.fn()}
        initialValue="a retained draft"
      />,
    );
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue(
      "a retained draft",
    );
  });

  it("reports composer edits through onDraftChange", async () => {
    const onDraftChange = vi.fn();
    render(
      <ChatPanel
        title="Doc chat"
        messages={messages}
        onSend={vi.fn()}
        onDraftChange={onDraftChange}
      />,
    );
    await userEvent.type(
      screen.getByRole("textbox", { name: "Message" }),
      "hi",
    );
    expect(onDraftChange).toHaveBeenCalledWith("hi");
  });

  it("has no accessibility violations when ready", async () => {
    const { container } = render(
      <ChatPanel title="Doc chat" messages={messages} onSend={vi.fn()} />,
    );
    await expectNoAccessibilityViolations(container);
  });

  it("disables the composer while the agent is stopped even without composerDisabled", () => {
    render(
      <ChatPanel
        title="Doc chat"
        messages={messages}
        onSend={vi.fn()}
        agentActivity="stopped"
      />,
    );
    expect(screen.getByText(/Agent stopped responding/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("passes the question message id as the inline answer's second argument", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    render(
      <ChatPanel
        title="Doc chat"
        messages={[
          {
            id: "q1",
            author: "assistant",
            content: "Proceed?",
            pending: true,
            question: { responseType: "boolean" },
          },
        ]}
        onSend={onSend}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Yes" }));
    expect(onSend).toHaveBeenCalledWith("true", "q1");
  });

  it("handles a rejected inline answer without an unhandled rejection", async () => {
    // No @types/node in this package, so the process listener is reached
    // through a narrow structural cast.
    const proc = (
      globalThis as {
        process?: {
          on(
            event: "unhandledRejection",
            listener: (reason: unknown) => void,
          ): void;
          off(
            event: "unhandledRejection",
            listener: (reason: unknown) => void,
          ): void;
        };
      }
    ).process;
    expect(proc).toBeDefined();
    const onUnhandled = vi.fn();
    proc?.on("unhandledRejection", onUnhandled);
    try {
      const onSend = vi.fn().mockRejectedValue(new Error("send failed"));
      render(
        <ChatPanel
          title="Doc chat"
          messages={[
            {
              id: "q1",
              author: "assistant",
              content: "Proceed?",
              pending: true,
              question: { responseType: "boolean" },
            },
          ]}
          onSend={onSend}
        />,
      );
      await userEvent.click(screen.getByRole("button", { name: "No" }));
      expect(onSend).toHaveBeenCalledWith("false", "q1");
      await new Promise((resolve) => setTimeout(resolve, 10));
      expect(onUnhandled).not.toHaveBeenCalled();
    } finally {
      proc?.off("unhandledRejection", onUnhandled);
    }
  });
});
