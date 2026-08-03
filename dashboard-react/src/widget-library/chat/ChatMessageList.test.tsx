import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../test/setup";
import { ChatMessageList } from "./ChatMessageList";
import type { ChatMessage } from "./contracts";

// jsdom performs no layout, so scroll geometry is installed explicitly. scrollTop
// is backed by a real variable so the component's writes are observable.
function installScroll(
  element: HTMLElement,
  geometry: { scrollHeight: number; clientHeight: number; scrollTop?: number },
) {
  let top = geometry.scrollTop ?? 0;
  Object.defineProperty(element, "scrollTop", {
    configurable: true,
    get: () => top,
    set: (value: number) => {
      top = value;
    },
  });
  Object.defineProperty(element, "scrollHeight", {
    configurable: true,
    get: () => geometry.scrollHeight,
  });
  Object.defineProperty(element, "clientHeight", {
    configurable: true,
    get: () => geometry.clientHeight,
  });
}

const msg = (id: string, content: string, author: ChatMessage["author"]): ChatMessage => ({
  id,
  author,
  content,
});

describe("ChatMessageList", () => {
  it("renders author-attributed messages with a timestamp", () => {
    render(
      <ChatMessageList
        messages={[
          { id: "m1", author: "user", content: "Hi", createdAt: "2026-07-17T12:00:00-04:00" },
          { id: "m2", author: "assistant", content: "Hello" },
        ]}
      />,
    );
    expect(screen.getByText("Hi")).toBeInTheDocument();
    expect(screen.getByText("Hello")).toBeInTheDocument();
    expect(screen.getByText("You:")).toBeInTheDocument();
    expect(screen.getByText("Assistant:")).toBeInTheDocument();
  });

  it("shows the empty label when there are no messages", () => {
    render(<ChatMessageList messages={[]} emptyLabel="Nothing yet." />);
    expect(screen.getByText("Nothing yet.")).toBeInTheDocument();
  });

  it("shows immutable producer metadata beside an assistant turn", () => {
    render(
      <ChatMessageList
        messages={[
          {
            id: "m1",
            author: "assistant",
            content: "Generated reply",
            producer: {
              providerId: "codex",
              modelId: "gpt-5.6",
              providerLabel: "Codex",
              modelLabel: "GPT-5.6",
            },
          },
        ]}
      />,
    );

    expect(
      screen.getByLabelText("Produced by Codex, GPT-5.6"),
    ).toHaveTextContent("Codex · GPT-5.6");
  });

  it("autoscrolls to the newest message while pinned to the bottom", () => {
    const initial = [msg("m1", "one", "assistant")];
    const { rerender } = render(<ChatMessageList messages={initial} />);
    const log = screen.getByRole("log");
    installScroll(log, { scrollHeight: 500, clientHeight: 100, scrollTop: 0 });

    rerender(
      <ChatMessageList messages={[...initial, msg("m2", "two", "assistant")]} />,
    );

    expect(log.scrollTop).toBe(500);
    expect(
      screen.queryByRole("button", { name: /Jump to latest/ }),
    ).not.toBeInTheDocument();
  });

  it("locks scroll and shows an unread boundary when the reader has scrolled up", async () => {
    const onReachLatest = vi.fn();
    const initial = [msg("m1", "one", "assistant")];
    const { rerender } = render(
      <ChatMessageList messages={initial} onReachLatest={onReachLatest} />,
    );
    const log = screen.getByRole("log");
    installScroll(log, { scrollHeight: 500, clientHeight: 100, scrollTop: 0 });
    fireEvent.scroll(log);

    rerender(
      <ChatMessageList
        messages={[
          ...initial,
          msg("m2", "two", "assistant"),
          msg("m3", "three", "assistant"),
        ]}
        onReachLatest={onReachLatest}
      />,
    );

    // Scroll lock holds position and surfaces the unread affordances.
    expect(log.scrollTop).toBe(0);
    expect(screen.getByRole("separator", { name: /unread/i })).toBeInTheDocument();
    const jump = screen.getByRole("button", { name: /2 new messages/ });

    await userEvent.click(jump);

    expect(log.scrollTop).toBe(500);
    expect(
      screen.queryByRole("button", { name: /Jump to latest/ }),
    ).not.toBeInTheDocument();
    expect(onReachLatest).toHaveBeenCalledTimes(1);
  });

  it("opens locked at the seeded unread boundary rather than the bottom", () => {
    render(
      <ChatMessageList
        messages={[
          msg("m1", "read one", "assistant"),
          msg("m2", "unread one", "assistant"),
          msg("m3", "unread two", "assistant"),
        ]}
        initialUnreadFromMessageId="m2"
      />,
    );
    expect(screen.getByRole("separator", { name: /unread/i })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /2 new messages/ }),
    ).toBeInTheDocument();
  });

  it("opens at the top with the separator above the first message when the whole transcript is unread", () => {
    const scrollIntoView = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrollIntoView;
    try {
      render(
        <ChatMessageList
          messages={[
            msg("m1", "unread one", "assistant"),
            msg("m2", "unread two", "assistant"),
          ]}
          initialUnreadFromMessageId="m1"
        />,
      );
      const separator = screen.getByRole("separator", { name: /unread/i });
      const log = screen.getByRole("log");
      // Boundary at index 0 renders above the first message.
      expect(log.firstElementChild).toBe(separator);
      // The mount position is the boundary, which sits at the top.
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "start" });
      expect(scrollIntoView.mock.contexts[0]).toBe(separator);
      expect(
        screen.getByRole("button", { name: /2 new messages/ }),
      ).toBeInTheDocument();
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });

  it("answers a pending choice question inline", async () => {
    const onRespond = vi.fn();
    render(
      <ChatMessageList
        messages={[
          {
            id: "q1",
            author: "assistant",
            content: "Which one?",
            pending: true,
            question: {
              responseType: "choice",
              choices: [
                { key: "a", label: "Option A" },
                { key: "b", label: "Option B" },
              ],
            },
          },
        ]}
        onRespond={onRespond}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Option B" }));
    expect(onRespond).toHaveBeenCalledWith("b", "q1");
  });

  it("answers a pending boolean question inline", async () => {
    const onRespond = vi.fn();
    render(
      <ChatMessageList
        messages={[
          {
            id: "q1",
            author: "assistant",
            content: "Proceed?",
            pending: true,
            question: { responseType: "boolean" },
          },
        ]}
        onRespond={onRespond}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Yes" }));
    expect(onRespond).toHaveBeenCalledWith("true", "q1");
  });

  it("renders additive message accessories before questions and an appendix after messages", () => {
    render(
      <ChatMessageList
        messages={[
          {
            id: "q1",
            author: "assistant",
            content: "Proceed?",
            pending: true,
            question: { responseType: "boolean" },
          },
        ]}
        renderMessageAccessory={(message) => (
          <span data-testid={`accessory-${message.id}`}>Message accessory</span>
        )}
        transcriptAppendix={<div data-testid="appendix">Transcript appendix</div>}
        onRespond={vi.fn()}
      />,
    );

    const content = screen.getByText("Proceed?");
    const accessory = screen.getByTestId("accessory-q1");
    const choices = screen.getByRole("button", { name: "Yes" }).parentElement!;
    const message = content.closest(".wb-chat-msg")!;
    const appendix = screen.getByTestId("appendix");
    expect(content.compareDocumentPosition(accessory)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(accessory.compareDocumentPosition(choices)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(message.compareDocumentPosition(appendix)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it("disables every structured response without dropping its question identity", async () => {
    const onRespond = vi.fn();
    render(
      <ChatMessageList
        messages={[
          {
            id: "q-boolean",
            author: "assistant",
            content: "Proceed?",
            pending: true,
            question: { responseType: "boolean" },
          },
          {
            id: "q-choice",
            author: "assistant",
            content: "Which?",
            pending: true,
            question: {
              responseType: "choice",
              choices: [{ key: "a", label: "Option A" }],
            },
          },
        ]}
        onRespond={onRespond}
        responsesDisabled
      />,
    );

    for (const name of ["Yes", "No", "Option A"]) {
      const response = screen.getByRole("button", { name });
      expect(response).toBeDisabled();
      await userEvent.click(response);
    }
    expect(onRespond).not.toHaveBeenCalled();
  });

  it("shows the working and terminal no-response states", () => {
    const { rerender } = render(
      <ChatMessageList
        messages={[msg("m1", "working", "assistant")]}
        agentActivity="thinking"
      />,
    );
    expect(screen.getByText("Assistant is typing")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Assistant is typing");

    rerender(
      <ChatMessageList
        messages={[msg("m1", "working", "assistant")]}
        agentActivity="stopped"
      />,
    );
    expect(screen.getByText("No response received.")).toBeInTheDocument();
  });

  it("allows a host to suppress the passive no-response notice", () => {
    render(
      <ChatMessageList
        messages={[msg("m1", "working", "assistant")]}
        agentActivity="stopped"
        showStoppedNotice={false}
      />,
    );
    expect(screen.queryByText("No response received.")).toBeNull();
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <ChatMessageList
        messages={[
          msg("m1", "one", "user"),
          msg("m2", "two", "assistant"),
        ]}
        agentActivity="thinking"
        renderMessageAccessory={() => <button type="button">Open source</button>}
        transcriptAppendix={<div role="status">One delivery notice</div>}
      />,
    );
    await expectNoAccessibilityViolations(container);
  });
});
