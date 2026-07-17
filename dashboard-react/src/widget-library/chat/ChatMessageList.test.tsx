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

  it("shows the typing indicator and the agent-stopped notice", () => {
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
    expect(screen.getByText(/Agent stopped responding/)).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <ChatMessageList
        messages={[
          msg("m1", "one", "user"),
          msg("m2", "two", "assistant"),
        ]}
        agentActivity="thinking"
      />,
    );
    await expectNoAccessibilityViolations(container);
  });
});
