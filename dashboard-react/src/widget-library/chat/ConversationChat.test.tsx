import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../test/setup";
import { ConversationChat } from "./ConversationChat";
import { InMemoryChatProvider } from "./InMemoryChatProvider";
import type {
  ChatConversationProvider,
  ChatConversationSnapshot,
  ChatMessage,
  ChatSendInput,
} from "./contracts";

const assistantMessage: ChatMessage = {
  id: "a1",
  author: "assistant",
  content: "How can I help?",
};

function provider(
  conversationId: string,
  init: Partial<ChatConversationSnapshot> = {},
): InMemoryChatProvider {
  return new InMemoryChatProvider({
    conversationId,
    status: init.status ?? "open",
    agentLiveness: init.agentLiveness ?? "alive",
    messages: init.messages ?? [assistantMessage],
  });
}

describe("ConversationChat", () => {
  it("owns provider loading and reports the canonical messages", async () => {
    const onMessagesChange = vi.fn();
    render(
      <ConversationChat
        provider={provider("c1")}
        conversationId="c1"
        title="Project chat"
        onMessagesChange={onMessagesChange}
      />,
    );

    expect(await screen.findByText("How can I help?")).toBeVisible();
    await waitFor(() =>
      expect(onMessagesChange).toHaveBeenLastCalledWith([assistantMessage]),
    );
  });

  it("retries a failed load through the shared panel", async () => {
    const snapshot: ChatConversationSnapshot = {
      conversationId: "c1",
      status: "open",
      agentLiveness: "alive",
      messages: [assistantMessage],
    };
    const loadConversation = vi
      .fn()
      .mockRejectedValueOnce(new Error("Chat service unavailable."))
      .mockResolvedValue(snapshot);
    const chatProvider: ChatConversationProvider = {
      loadConversation,
      sendMessage: vi.fn(async () => snapshot),
      subscribe: vi.fn(() => () => {}),
    };
    render(
      <ConversationChat
        provider={chatProvider}
        conversationId="c1"
        title="Project chat"
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Chat service unavailable.",
    );
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("How can I help?")).toBeVisible();
    expect(loadConversation).toHaveBeenCalledTimes(2);
  });

  it("preserves a structured question's message id through the reusable surface", async () => {
    const question: ChatMessage = {
      id: "question-7",
      author: "assistant",
      content: "Proceed?",
      pending: true,
      question: { responseType: "boolean" },
    };
    const chatProvider = provider("c1", { messages: [question] });
    const sendMessage = vi.spyOn(chatProvider, "sendMessage");
    render(
      <ConversationChat
        provider={chatProvider}
        conversationId="c1"
        title="Project chat"
      />,
    );

    await userEvent.click(
      await screen.findByRole("button", { name: "Yes" }),
    );
    expect(sendMessage).toHaveBeenCalledWith("c1", {
      value: "true",
      inReplyTo: "question-7",
      messageId: expect.stringMatching(/^chat-user-/),
    });
  });

  it("maps a closed conversation to readable, response-disabled chat", async () => {
    const question: ChatMessage = {
      id: "question-7",
      author: "assistant",
      content: "Proceed?",
      pending: true,
      question: { responseType: "boolean" },
    };
    render(
      <ConversationChat
        provider={provider("c1", {
          status: "closed",
          messages: [question],
        })}
        conversationId="c1"
        title="Project chat"
      />,
    );

    expect(
      await screen.findByText("This conversation is closed."),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Yes" })).toBeDisabled();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("keeps feature-specific document context out of the shared surface", async () => {
    render(
      <ConversationChat
        provider={provider("c1")}
        conversationId="c1"
        title="Project chat"
      />,
    );

    expect(await screen.findByText("How can I help?")).toBeVisible();
    expect(screen.queryByText(/Working on/i)).toBeNull();
    expect(screen.queryByText(/Frozen version/i)).toBeNull();
    expect(screen.queryByText(/^About:/i)).toBeNull();
  });

  it("resets internally-owned draft state when the conversation identity changes", async () => {
    const first = provider("c1");
    const second = provider("c2");
    const { rerender } = render(
      <ConversationChat
        provider={first}
        conversationId="c1"
        title="Project chat"
        initialValue="first draft"
      />,
    );
    const firstComposer = await screen.findByRole("textbox");
    await userEvent.type(firstComposer, " changed");
    expect(firstComposer).toHaveValue("first draft changed");

    rerender(
      <ConversationChat
        provider={second}
        conversationId="c2"
        title="Project chat"
        initialValue="second draft"
      />,
    );

    expect(await screen.findByRole("textbox")).toHaveValue("second draft");
  });

  it("surfaces a pre-send failure, retains the draft, and clears it on retry", async () => {
    const chatProvider = provider("c1", { messages: [] });
    const prepareSend = vi
      .fn()
      .mockRejectedValueOnce(
        new Error("The document changed while its exact context was prepared."),
      )
      .mockImplementation(async (input) => input);
    render(
      <ConversationChat
        provider={chatProvider}
        conversationId="c1"
        title="Project chat"
        prepareSend={prepareSend}
      />,
    );
    const input = await screen.findByRole("textbox");
    await userEvent.type(input, "Keep this authored draft");

    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(
      await screen.findByText(
        "The document changed while its exact context was prepared.",
      ),
    ).toBeVisible();
    expect(input).toHaveValue("Keep this authored draft");

    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(input).toHaveValue(""));
    expect(
      screen.queryByText(
        "The document changed while its exact context was prepared.",
      ),
    ).toBeNull();
    expect(prepareSend).toHaveBeenCalledTimes(2);
    expect(prepareSend.mock.calls[0]?.[0].messageId).toBe(
      prepareSend.mock.calls[1]?.[0].messageId,
    );
  });

  it("reuses the prepared envelope and message identity after an uncertain send", async () => {
    const snapshot: ChatConversationSnapshot = {
      conversationId: "c1",
      status: "open",
      agentLiveness: "alive",
      messages: [],
    };
    const sendMessage = vi
      .fn()
      .mockRejectedValueOnce(new Error("Acknowledgement was lost."))
      .mockResolvedValue(snapshot);
    const chatProvider: ChatConversationProvider = {
      loadConversation: vi.fn(async () => snapshot),
      sendMessage,
      subscribe: vi.fn(() => () => {}),
    };
    const prepareSend = vi.fn(async (input: ChatSendInput) => ({
      ...input,
      value: `${input.value} (prepared)`,
    }));
    render(
      <ConversationChat
        provider={chatProvider}
        conversationId="c1"
        title="Project chat"
        prepareSend={prepareSend}
      />,
    );
    const input = await screen.findByRole("textbox");
    await userEvent.type(input, "Keep this exact turn");

    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Acknowledgement was lost.")).toBeVisible();
    expect(input).toHaveValue("Keep this exact turn");

    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(input).toHaveValue(""));

    expect(prepareSend).toHaveBeenCalledTimes(1);
    expect(sendMessage).toHaveBeenCalledTimes(2);
    const first = sendMessage.mock.calls[0]?.[1];
    const second = sendMessage.mock.calls[1]?.[1];
    expect(first).toEqual(second);
    expect(first?.messageId).toMatch(/^chat-user-/);
    expect(first?.value).toBe("Keep this exact turn (prepared)");
  });

  it("has no accessibility violations with additive extensions", async () => {
    const { container } = render(
      <ConversationChat
        provider={provider("c1")}
        conversationId="c1"
        title="Project chat"
        renderMessageAccessory={() => (
          <button type="button">Open related item</button>
        )}
        transcriptAppendix={<div role="status">One background delivery</div>}
      />,
    );
    await screen.findByText("How can I help?");
    await expectNoAccessibilityViolations(container);
  });
});
