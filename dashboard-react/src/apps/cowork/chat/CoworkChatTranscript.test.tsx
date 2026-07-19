import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import type { ChatMessage } from "../../../widget-library/chat";
import { CoworkChatTranscript } from "./CoworkChatTranscript";
import type { ResolvedSpanLink, RoutingDelivery } from "./contracts";

// jsdom performs no layout, so scroll geometry is installed explicitly, the
// same technique the house ChatMessageList test uses.
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

const userMessage = (id: string, content: string): ChatMessage => ({
  id,
  author: "user",
  content,
});

describe("CoworkChatTranscript", () => {
  it("renders a feedback message with its span-link affordance", () => {
    const link: ResolvedSpanLink = {
      messageId: "u1",
      evidenceId: "ev-9",
      target: { spanId: "span-9", anchor: { exact: "too strong" } },
    };
    render(
      <CoworkChatTranscript
        messages={[userMessage("u1", "this claim is too strong")]}
        spanLinks={new Map([["u1", link]])}
        onScrollToAnchor={vi.fn()}
      />,
    );

    expect(screen.getByText("this claim is too strong")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Jump to the passage "too strong"/ }),
    ).toBeInTheDocument();
  });

  it("invokes the scroll-to callback with the span target", async () => {
    const target = { spanId: "span-9", anchor: { exact: "too strong" } };
    const onScrollToAnchor = vi.fn();
    render(
      <CoworkChatTranscript
        messages={[userMessage("u1", "this claim is too strong")]}
        spanLinks={
          new Map([
            ["u1", { messageId: "u1", evidenceId: "ev-9", target }],
          ])
        }
        onScrollToAnchor={onScrollToAnchor}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: /Jump to the passage/ }),
    );
    expect(onScrollToAnchor).toHaveBeenCalledWith(target);
  });

  it("disables the affordance when no scroll-to seam is wired", () => {
    render(
      <CoworkChatTranscript
        messages={[userMessage("u1", "anchored note")]}
        spanLinks={
          new Map([
            [
              "u1",
              {
                messageId: "u1",
                evidenceId: "ev-1",
                target: { spanId: "span-1" },
              },
            ],
          ])
        }
      />,
    );
    expect(
      screen.getByRole("button", { name: /Jump to the anchored passage/ }),
    ).toBeDisabled();
  });

  it("shows the typing indicator while the agent is thinking", () => {
    render(
      <CoworkChatTranscript
        messages={[{ id: "a1", author: "assistant", content: "..." }]}
        agentActivity="thinking"
      />,
    );
    expect(screen.getByText("Assistant is typing")).toBeInTheDocument();
  });

  it("shows the stopped notice when the agent has stopped", () => {
    render(
      <CoworkChatTranscript
        messages={[{ id: "a1", author: "assistant", content: "done" }]}
        agentActivity="stopped"
      />,
    );
    expect(screen.getByText(/Agent stopped responding/)).toBeInTheDocument();
  });

  it("accrues unread and clears it on jump to latest", () => {
    const { rerender } = render(
      <CoworkChatTranscript messages={[userMessage("u1", "first")]} />,
    );
    const log = screen.getByRole("log");
    // A tall scroll far from the bottom, so the reader is not pinned.
    installScroll(log, { scrollHeight: 1000, clientHeight: 200, scrollTop: 0 });
    fireEvent.scroll(log);

    rerender(
      <CoworkChatTranscript
        messages={[
          userMessage("u1", "first"),
          { id: "a1", author: "assistant", content: "a reply arrived" },
        ]}
      />,
    );

    const jump = screen.getByRole("button", { name: /Jump to latest/ });
    expect(jump).toHaveTextContent("1 new message");

    fireEvent.click(jump);
    expect(
      screen.queryByRole("button", { name: /Jump to latest/ }),
    ).not.toBeInTheDocument();
  });

  it("renders a delivered routing note and dismisses it", async () => {
    const routing: RoutingDelivery[] = [
      {
        id: "routing-1",
        verb: "redirect",
        proposalId: "p1",
        state: "delivered",
        note: "tighten the scope",
      },
    ];
    const onDismissRouting = vi.fn();
    render(
      <CoworkChatTranscript
        messages={[userMessage("u1", "tighten the scope")]}
        routing={routing}
        onDismissRouting={onDismissRouting}
      />,
    );

    expect(
      screen.getByText(/Redirect sent to the document agent/),
    ).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: /Dismiss redirect delivery notice/ }),
    );
    expect(onDismissRouting).toHaveBeenCalledWith("routing-1");
  });

  it("marks a failed routing note distinctly", () => {
    render(
      <CoworkChatTranscript
        messages={[]}
        routing={[
          {
            id: "routing-2",
            verb: "endorse",
            proposalId: "p2",
            state: "failed",
            reason: "conversation_unavailable",
          },
        ]}
      />,
    );
    expect(
      screen.getByText(/Endorsement could not be delivered/),
    ).toBeInTheDocument();
    expect(screen.getByText(/conversation_unavailable/)).toBeInTheDocument();
  });

  it("has no accessibility violations in a populated transcript", async () => {
    const { container } = render(
      <CoworkChatTranscript
        messages={[
          { id: "a1", author: "assistant", content: "I proposed an edit." },
          userMessage("u1", "this claim is too strong"),
        ]}
        agentActivity="thinking"
        spanLinks={
          new Map([
            [
              "u1",
              {
                messageId: "u1",
                evidenceId: "ev-9",
                target: { spanId: "span-9", anchor: { exact: "too strong" } },
              },
            ],
          ])
        }
        routing={[
          {
            id: "routing-1",
            verb: "endorse",
            proposalId: "p1",
            state: "delivered",
          },
        ]}
        onScrollToAnchor={vi.fn()}
        onDismissRouting={vi.fn()}
      />,
    );
    await expectNoAccessibilityViolations(container);
  });
});
