import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import { CoworkChatAnnotations } from "./annotations";
import { CoworkChatPanel } from "./CoworkChatPanel";
import { HttpChatConversationProvider } from "./HttpChatConversationProvider";

function jsonResponse(
  body: unknown,
  init?: { ok?: boolean; status?: number; statusText?: string },
): Response {
  const status = init?.status ?? 200;
  return {
    ok: init?.ok ?? (status >= 200 && status < 300),
    status,
    statusText: init?.statusText ?? "",
    json: async () => body,
  } as unknown as Response;
}

interface RawMessage {
  readonly message_id: string;
  readonly role: string;
  readonly content: string;
}

function conversation(
  messages: readonly RawMessage[],
  init?: { status?: string; agent_alive?: boolean | null },
) {
  return {
    conversation: {
      conversation_id: "c1",
      title: "Document conversation",
      status: init?.status ?? "open",
      agent_alive: init?.agent_alive ?? null,
    },
    messages,
  };
}

function provider(fetchImpl: typeof fetch) {
  return new HttpChatConversationProvider({
    conversationId: "c1",
    fetchImpl,
    pollIntervalMs: 0,
  });
}

describe("CoworkChatPanel", () => {
  it("renders the live transcript once loaded", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(
        conversation([{ message_id: "m1", role: "agent", content: "I proposed edits." }]),
      ),
    ) as unknown as typeof fetch;

    render(<CoworkChatPanel provider={provider(fetchImpl)} conversationId="c1" />);

    expect(await screen.findByText("I proposed edits.")).toBeInTheDocument();
  });

  it("renders the span-link affordance for a feedback capture", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(
        conversation([{ message_id: "u1", role: "user", content: "make this precise" }]),
      ),
    ) as unknown as typeof fetch;
    const annotations = new CoworkChatAnnotations();
    const onScrollToAnchor = vi.fn();

    render(
      <CoworkChatPanel
        provider={provider(fetchImpl)}
        conversationId="c1"
        annotations={annotations}
        onScrollToAnchor={onScrollToAnchor}
      />,
    );

    await screen.findByText("make this precise");
    expect(
      screen.queryByRole("button", { name: /Jump to the passage/ }),
    ).not.toBeInTheDocument();

    // The feedback entry point: R9 returned, the surface records it here.
    act(() => {
      annotations.annotateFeedback({
        evidenceId: "ev-1",
        spanId: "span-1",
        conversationId: "c1",
        text: "make this precise",
        anchor: { exact: "precise" },
      });
    });

    const jump = await screen.findByRole("button", {
      name: /Jump to the passage/,
    });
    await userEvent.click(jump);
    expect(onScrollToAnchor).toHaveBeenCalledWith({
      spanId: "span-1",
      anchor: { exact: "precise" },
    });
  });

  it("renders a routing-note delivery recorded by the submit path", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(conversation([])),
    ) as unknown as typeof fetch;
    const annotations = new CoworkChatAnnotations();

    render(
      <CoworkChatPanel
        provider={provider(fetchImpl)}
        conversationId="c1"
        annotations={annotations}
      />,
    );
    await screen.findByText(/No messages yet/);

    act(() => {
      annotations.annotateRoutingDelivery({
        verb: "redirect",
        proposalId: "p1",
        state: "delivered",
        note: "tighten the scope",
      });
    });

    expect(
      await screen.findByText(/Redirect sent to the document agent/),
    ).toBeInTheDocument();
  });

  it("sends a human turn through the transport and shows the reply", async () => {
    let posted = false;
    const fetchImpl = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        posted = true;
        return jsonResponse({ sent: true, message_id: "u1" });
      }
      return jsonResponse(
        conversation(
          posted
            ? [
                { message_id: "u1", role: "user", content: "tighten this" },
                { message_id: "a1", role: "agent", content: "Done." },
              ]
            : [],
        ),
      );
    }) as unknown as typeof fetch;

    render(<CoworkChatPanel provider={provider(fetchImpl)} conversationId="c1" />);
    await screen.findByText(/No messages yet/);

    await userEvent.type(screen.getByRole("textbox"), "tighten this");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("tighten this")).toBeInTheDocument();
    expect(await screen.findByText("Done.")).toBeInTheDocument();
  });

  it("shows a read-only notice and no composer on a closed conversation", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(conversation([], { status: "closed" })),
    ) as unknown as typeof fetch;

    render(<CoworkChatPanel provider={provider(fetchImpl)} conversationId="c1" />);

    expect(
      await screen.findByText(/This conversation is closed/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Send" }),
    ).not.toBeInTheDocument();
  });

  it("surfaces a load error and recovers on retry", async () => {
    let attempts = 0;
    const fetchImpl = vi.fn(async () => {
      attempts += 1;
      if (attempts === 1) {
        return jsonResponse({ error: "store is not reachable" }, { status: 404 });
      }
      return jsonResponse(
        conversation([{ message_id: "m1", role: "agent", content: "recovered" }]),
      );
    }) as unknown as typeof fetch;

    render(<CoworkChatPanel provider={provider(fetchImpl)} conversationId="c1" />);

    await screen.findByText(/Conversation could not load/);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("recovered")).toBeInTheDocument();
  });

  it("has no accessibility violations in the ready state", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(
        conversation([{ message_id: "m1", role: "agent", content: "Ready." }]),
      ),
    ) as unknown as typeof fetch;

    const { container } = render(
      <CoworkChatPanel provider={provider(fetchImpl)} conversationId="c1" />,
    );
    await screen.findByText("Ready.");
    await waitFor(() => expect(container).toBeTruthy());
    await expectNoAccessibilityViolations(container);
  });
});
