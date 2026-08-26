import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../test/setup";
import {
  ChatPanel,
  ChatPanelState,
  type ChatPanelProps,
} from "./ChatPanel";
import type { ChatMessage } from "./contracts";
import type { ChatExecutionControl } from "./useChatExecutionProfile";

const messages: ChatMessage[] = [
  { id: "m1", author: "user", content: "Hi" },
  { id: "m2", author: "assistant", content: "Hello" },
];

const pendingQuestion: ChatMessage = {
  id: "q1",
  author: "assistant",
  content: "Proceed?",
  pending: true,
  question: { responseType: "boolean" },
};

const executionControl = (
  overrides: Partial<ChatExecutionControl> = {},
): ChatExecutionControl => ({
  snapshot: {
    selection: {
      providerId: "claude-code",
      modelId: "sonnet",
      providerLabel: "Claude Code",
      modelLabel: "Sonnet",
      revision: "execution:1",
    },
    providers: [
      {
        id: "claude-code",
        label: "Claude Code",
        available: true,
        models: [{ id: "sonnet", label: "Sonnet", available: true }],
      },
    ],
  },
  status: "ready",
  selecting: false,
  error: null,
  announcement: null,
  currentAvailable: true,
  select: vi.fn(async () => {}),
  retry: vi.fn(),
  ...overrides,
});

describe("ChatPanel", () => {
  it("forwards a host primary action without authorizing inline answers", async () => {
    const onAction = vi.fn();
    const onSend = vi.fn();
    render(<ChatPanel messages={[pendingQuestion]} onSend={onSend} composerPrimaryAction={{ label: "Launch", disabled: false, onAction }} />);
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Yes" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "No" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Launch" }));
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onSend).not.toHaveBeenCalled();
  });

  it("never exposes a host launch action in a read-only conversation", () => {
    render(<ChatPanel status="read-only" messages={messages} onSend={vi.fn()} composerPrimaryAction={{ label: "Launch", disabled: false, onAction: vi.fn() }} />);
    expect(screen.queryByRole("button", { name: "Launch" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByText("Hi")).toBeVisible();
  });

  it.each(["loading", "empty", "error"] as const)("keeps a pending host action locked and absent from the %s fallback", (status) => {
    render(<ChatPanel status={status} messages={[]} onSend={vi.fn()} execution={executionControl()} executionDisabled={false} composerPrimaryAction={{ label: "Launch", pendingLabel: "Launching…", pending: true, disabled: false, onAction: vi.fn() }} onRetry={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /Launch/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Run with/ })).toBeDisabled();
    if (status === "error") expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
  });

  it.each(["loading", "empty", "error"] as const)("preserves host model locks in the %s fallback", (status) => {
    const { rerender } = render(<ChatPanel status={status} messages={[]} onSend={vi.fn()} composerDisabled execution={executionControl()} executionDisabled />);
    expect(screen.getByRole("button", { name: /Run with/ })).toBeDisabled();
    rerender(<ChatPanel status={status} messages={[]} onSend={vi.fn()} composerDisabled execution={executionControl()} executionDisabled={false} />);
    expect(screen.getByRole("button", { name: /Run with/ })).toBeEnabled();
    rerender(<ChatPanel status={status} messages={[]} onSend={vi.fn()} composerDisabled sending execution={executionControl()} executionDisabled={false} />);
    expect(screen.getByRole("button", { name: /Run with/ })).toBeDisabled();
  });

  it("honors model locks in the standalone no-composer presentation", () => {
    render(<ChatPanel messages={[]} execution={executionControl()} executionDisabled />);
    expect(screen.getByRole("button", { name: /Run with/ })).toBeDisabled();
  });
  it("preserves host-disabled input while exposing the canonical pre-Start picker", () => {
    render(<ChatPanel messages={[]} onSend={vi.fn()} composerDisabled execution={executionControl()} executionDisabled={false} />);
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Run with/ })).toBeEnabled();
  });

  it("keeps the composer and model picker interactive during passive first-turn feedback", async () => {
    render(
      <ChatPanel
        messages={[]}
        onSend={vi.fn()}
        agentActivity="starting"
        execution={executionControl()}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Assistant is typing");
    expect(screen.getByRole("button", { name: /Run with/ })).toBeEnabled();
    await userEvent.type(
      screen.getByRole("textbox", { name: "Message" }),
      "Add another detail",
    );
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
  });
  it("renders the title header, transcript, and composer when ready", () => {
    render(<ChatPanel title="Doc chat" messages={messages} onSend={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Doc chat" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy chat" })).toBeInTheDocument();
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
    expect(screen.getByRole("button", { name: "Copy chat" })).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Doc chat" }),
    ).not.toBeInTheDocument();
  });

  it("does not offer a transcript copy action before there are messages", () => {
    render(<ChatPanel title="Doc chat" messages={[]} onSend={vi.fn()} />);
    expect(
      screen.queryByRole("button", { name: "Copy chat" }),
    ).not.toBeInTheDocument();
  });

  it("lets a composed host place the same transcript action in its outer header", () => {
    render(
      <ChatPanel
        title="Doc chat"
        messages={messages}
        onSend={vi.fn()}
        showTranscriptCopyAction={false}
      />,
    );
    expect(
      screen.queryByRole("button", { name: "Copy chat" }),
    ).not.toBeInTheDocument();
  });

  it("forwards additive transcript extensions without replacing canonical content", () => {
    render(
      <ChatPanel
        title="Doc chat"
        messages={[pendingQuestion]}
        onSend={vi.fn()}
        renderMessageAccessory={(message) => (
          <button type="button">Open {message.id}</button>
        )}
        transcriptAppendix={<div role="status">One delivery notice</div>}
      />,
    );
    expect(screen.getByText("Proceed?")).toBeVisible();
    expect(screen.getByRole("button", { name: "Open q1" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Yes" })).toBeVisible();
    expect(screen.getByText("One delivery notice")).toBeVisible();
  });

  it("shows the loading host state", () => {
    render(<ChatPanel status="loading" messages={[]} />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading chat");
    expect(screen.getByRole("status")).toHaveTextContent("Loading messages.");
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

  it("does not couple a generic transcript retry to model discovery", async () => {
    const onRetry = vi.fn();
    render(
      <ChatPanel
        status="error"
        messages={[]}
        onRetry={onRetry}
        execution={executionControl({
          status: "error",
          currentAvailable: false,
          error: "Model discovery is offline.",
        })}
      />,
    );

    const retry = screen.getByRole("button", { name: "Retry" });
    expect(retry).toBeEnabled();
    await userEvent.click(retry);
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("offers one reusable pre-conversation state and action", async () => {
    const onStart = vi.fn();
    render(
      <ChatPanelState
        label="Document chat"
        kind="empty"
        title="Chat hasn’t started."
        detail="Start chat to ask about this document."
        action={{ label: "Start chat", onAction: onStart }}
      />,
    );

    expect(
      screen.getByRole("region", { name: "Document chat" }),
    ).toHaveTextContent("Chat hasn’t started.");
    await userEvent.click(screen.getByRole("button", { name: "Start chat" }));
    expect(onStart).toHaveBeenCalledOnce();
  });

  it("lets a host opt a launch action into execution availability", () => {
    render(
      <ChatPanelState
        kind="empty"
        title="Chat hasn’t started."
        action={{
          label: "Start chat",
          onAction: vi.fn(),
          requiresExecution: true,
        }}
        execution={executionControl({
          status: "error",
          currentAvailable: false,
        })}
      />,
    );

    expect(
      screen.getByRole("button", { name: "Start chat" }),
    ).toBeDisabled();
  });

  it("disables an execution-required action for a server read-only profile", () => {
    const execution = executionControl();
    render(
      <ChatPanelState
        kind="empty"
        title="Chat hasn’t started."
        action={{
          label: "Start chat",
          onAction: vi.fn(),
          requiresExecution: true,
        }}
        execution={{
          ...execution,
          snapshot: {
            ...execution.snapshot!,
            readOnly: true,
          },
        }}
      />,
    );

    expect(
      screen.getByRole("button", { name: "Start chat" }),
    ).toBeDisabled();
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

  it("honors server-authoritative execution read-only state", () => {
    render(
      <ChatPanel
        title="Archived"
        messages={messages}
        onSend={vi.fn()}
        execution={executionControl({
          snapshot: {
            ...executionControl().snapshot!,
            readOnly: true,
          },
        })}
      />,
    );

    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByText(/Read-only:/)).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /Run with/ }),
    ).not.toBeInTheDocument();
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

  it("shows terminal no-response while keeping the composer available", async () => {
    render(
      <ChatPanel
        title="Doc chat"
        messages={messages}
        onSend={vi.fn()}
        agentActivity="stopped"
      />,
    );
    expect(screen.getByText("No response received.")).toBeInTheDocument();
    const composer = screen.getByRole("textbox", { name: "Message" });
    expect(composer).toBeEnabled();
    await userEvent.type(composer, "Try again");
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
  });

  type LockedResponseProps = Omit<
    Partial<ChatPanelProps>,
    "messages" | "title" | "onSend"
  >;
  const lockedResponseCases: ReadonlyArray<
    readonly [string, LockedResponseProps]
  > = [
    ["read-only", { status: "read-only" }],
    ["waiting-for-response", { agentActivity: "thinking" }],
    ["sending", { sending: true }],
    ["composer-disabled", { composerDisabled: true }],
  ];

  it.each(lockedResponseCases)(
    "disables structured responses while the input is %s",
    (_name, lockedProps) => {
      render(
        <ChatPanel
          title="Doc chat"
          messages={[pendingQuestion]}
          onSend={vi.fn()}
          {...lockedProps}
        />,
      );
      expect(screen.getByRole("button", { name: "Yes" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "No" })).toBeDisabled();
    },
  );

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
