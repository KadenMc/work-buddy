import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { HttpChatConversationProvider } from "../../../dashboard/conversations";
import { expectNoAccessibilityViolations } from "../../../test/setup";
import type { ChatActionSnapshotContext } from "../../../widget-library/chat";
import type {
  CoworkActionSnapshotController,
  CoworkCapturedActionSnapshot,
} from "../targets";
import { CoworkChatAnnotations } from "./annotations";
import { CoworkChatPanel } from "./CoworkChatPanel";
import { CoworkChatTargetingProvider } from "./CoworkChatTargeting";

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
  readonly message_type?: string;
  readonly status?: string;
  readonly response_type?: string;
  readonly context?: Record<string, unknown>;
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
      screen.queryByRole("button", { name: /Jump to passage:/ }),
    ).not.toBeInTheDocument();

    // The feedback entry point: R9 returned, the surface records it here.
    act(() => {
      annotations.annotateFeedback({
        documentId: "doc-1",
        storeId: "store-1",
        evidenceId: "ev-1",
        spanId: "span-1",
        conversationId: "c1",
        messageId: "u1",
        text: "make this precise",
        anchor: { exact: "precise" },
      });
    });

    const jump = await screen.findByRole("button", {
      name: /Jump to passage:/,
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

  it("uses the shared Working on target and saves its frozen context even while Chat is stopped", async () => {
    const captured = {
      schema: "wb.cowork.action-snapshot/v1",
      storeId: "store-1",
      documentId: "doc-1",
    } as CoworkCapturedActionSnapshot;
    const targetSnapshot = {
      phase: "ready" as const,
      selection: null,
      currentSection: null,
      workingTarget: {
        kind: "text_range" as const,
        label: "Introduction",
        wordCount: 24,
        range: { from: 1, to: 30 },
      },
    };
    const controller: CoworkActionSnapshotController = {
      subscribe: () => () => undefined,
      getSnapshot: () => targetSnapshot,
      setWorkingTargetFromSelection: vi.fn(),
      clearWorkingTarget: vi.fn(),
      capture: vi.fn(async () => captured),
    };
    const context: ChatActionSnapshotContext = {
      kind: "action_snapshot",
      actionSnapshotId: "action-snapshot-12345678",
      storeId: "store-1",
      documentId: "doc-1",
      targetKind: "text_quote",
      targetLabel: "Introduction",
      targetWordCount: 24,
      targetTextSha256: "a".repeat(64),
      projectionSha256: "b".repeat(64),
      capturedAt: "2026-07-28T12:00:00Z",
    };
    const prepare = vi.fn(async () => context);
    let posted = false;
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        posted = true;
        return jsonResponse({ sent: true, message_id: "targeted-user" });
      }
      return jsonResponse(
        conversation(
          posted
            ? [
                {
                  message_id: "targeted-user",
                  role: "user",
                  content: "Focus here.",
                  context: {
                    kind: "action_snapshot",
                    action_snapshot_id: context.actionSnapshotId,
                    store_id: context.storeId,
                    document_id: context.documentId,
                    target_kind: context.targetKind,
                    target_label: context.targetLabel,
                    target_word_count: context.targetWordCount,
                    target_text_sha256: context.targetTextSha256,
                    projection_sha256: context.projectionSha256,
                    captured_at: context.capturedAt,
                  },
                },
              ]
            : [],
        ),
      );
    });
    const fetchImpl = fetchMock as unknown as typeof fetch;

    render(
      <CoworkChatTargetingProvider
        storeId="store-1"
        documentId="doc-1"
        controller={controller}
        agent={{
          status: "stopped",
          alive: false,
          started: false,
          error: null,
        }}
        client={{ prepare }}
      >
        <CoworkChatPanel
          provider={provider(fetchImpl)}
          conversationId="c1"
          agent={{
            status: "stopped",
            alive: false,
            started: false,
            error: null,
          }}
        />
      </CoworkChatTargetingProvider>,
    );
    await screen.findByText(/No messages yet/);
    expect(
      screen.getByLabelText(
        "About: Introduction. An exact version will be captured when sent.",
      ),
    ).toHaveTextContent("About: Introduction · 24 words");
    await userEvent.type(screen.getByRole("textbox"), "Focus here.");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await screen.findByText("Focus here.");
    expect(controller.capture).toHaveBeenCalledWith("working_target");
    expect(prepare).toHaveBeenCalledWith(captured);
    const post = fetchMock.mock.calls.find(
      (call) => call[1]?.method === "POST",
    );
    expect(JSON.parse((post?.[1] as RequestInit).body as string)).toEqual({
      value: "Focus here.",
      context: {
        kind: "action_snapshot",
        action_snapshot_id: context.actionSnapshotId,
        store_id: "store-1",
        document_id: "doc-1",
      },
    });
    expect(
      screen.getByLabelText("Frozen document context: Introduction"),
    ).toHaveTextContent("Frozen version · action-s");
  });

  it("explains when Working on context cannot be consumed", async () => {
    const targetSnapshot = {
      phase: "ready" as const,
      selection: null,
      currentSection: null,
      workingTarget: {
        kind: "unresolved" as const,
        label: "Old selection",
        wordCount: 0,
        range: null,
        resolution: "unresolved" as const,
      },
    };
    const controller: CoworkActionSnapshotController = {
      subscribe: () => () => undefined,
      getSnapshot: () => targetSnapshot,
      setWorkingTargetFromSelection: vi.fn(),
      clearWorkingTarget: vi.fn(),
      capture: vi.fn(),
    };
    const fetchImpl = vi.fn(async () =>
      jsonResponse(conversation([])),
    ) as unknown as typeof fetch;
    render(
      <CoworkChatTargetingProvider
        storeId="store-1"
        documentId="doc-1"
        controller={controller}
        agent={{
          status: "running",
          alive: true,
          started: true,
          error: null,
        }}
        client={{ prepare: vi.fn() }}
      >
        <CoworkChatPanel
          provider={provider(fetchImpl)}
          conversationId="c1"
          agent={{
            status: "running",
            alive: true,
            started: true,
            error: null,
          }}
        />
      </CoworkChatTargetingProvider>,
    );

    expect(
      await screen.findByLabelText(
        "Message target unavailable. Working on needs attention in the editor before Chat can use it.",
      ),
    ).toHaveTextContent("About: target unavailable");
  });

  it("clears the draft after an acknowledged send when its reload fails", async () => {
    let initialLoaded = false;
    const fetchImpl = vi.fn(
      async (_url: string, init?: RequestInit) => {
        if (init?.method === "POST") {
          return jsonResponse({ sent: true, message_id: "acknowledged-user" });
        }
        if (!initialLoaded) {
          initialLoaded = true;
          return jsonResponse(conversation([]));
        }
        return jsonResponse(
          { error: "temporarily unavailable" },
          { status: 503 },
        );
      },
    ) as unknown as typeof fetch;

    render(
      <CoworkChatPanel
        provider={provider(fetchImpl)}
        conversationId="c1"
      />,
    );
    await screen.findByText(/No messages yet/);
    const composer = screen.getByRole("textbox");
    await userEvent.type(composer, "keep this once");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("keep this once")).toBeVisible();
    expect(composer).toHaveValue("");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows a read-only notice and no composer on a closed conversation", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(conversation([], { status: "closed" })),
    ) as unknown as typeof fetch;

    render(<CoworkChatPanel provider={provider(fetchImpl)} conversationId="c1" />);

    expect(
      await screen.findByText(/This chat is closed/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Send" }),
    ).not.toBeInTheDocument();
  });

  it("shows a recoverable unavailable state when the document agent could not start", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(
        conversation([
          { message_id: "u1", role: "user", content: "Please review this." },
        ]),
      ),
    ) as unknown as typeof fetch;
    const onEnsureAgent = vi.fn();

    render(
      <CoworkChatPanel
        provider={provider(fetchImpl)}
        conversationId="c1"
        agent={{
          status: "spawn_failed",
          alive: false,
          started: false,
          error: "The configured agent runner is unavailable.",
        }}
        onEnsureAgent={onEnsureAgent}
      />,
    );

    expect(await screen.findByText("Please review this.")).toBeVisible();
    expect(screen.getByText("Chat couldn’t start.")).toBeVisible();
    expect(screen.queryByText("Chat paused.")).toBeNull();
    expect(screen.getByText(/configured agent runner is unavailable/i)).toBeVisible();
    expect(
      screen.getByRole("textbox", { name: "Message" }),
    ).toBeVisible();
    await userEvent.click(
      screen.getByRole("button", { name: "Try again" }),
    );
    expect(onEnsureAgent).toHaveBeenCalledOnce();
  });

  it("turns dynamic agent liveness failure into Restart on the same conversation", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(
        conversation(
          [{ message_id: "u1", role: "user", content: "Continue here." }],
          { agent_alive: false },
        ),
      ),
    ) as unknown as typeof fetch;
    const onEnsureAgent = vi.fn();

    render(
      <CoworkChatPanel
        provider={provider(fetchImpl)}
        conversationId="c1"
        agent={{
          status: "running",
          alive: true,
          started: false,
          error: null,
        }}
        onEnsureAgent={onEnsureAgent}
      />,
    );

    expect(await screen.findByText(/Chat paused/)).toBeVisible();
    expect(screen.getByText(/messages are still here/i)).toBeVisible();
    expect(screen.queryByText(/Close this chat/i)).toBeNull();
    await userEvent.click(
      screen.getByRole("button", { name: "Restart chat" }),
    );
    expect(onEnsureAgent).toHaveBeenCalledOnce();
  });

  it("keeps the transcript visible while a restart is in flight", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(
        conversation([
          { message_id: "u1", role: "user", content: "Keep this visible." },
        ]),
      ),
    ) as unknown as typeof fetch;

    render(
      <CoworkChatPanel
        provider={provider(fetchImpl)}
        conversationId="c1"
        agent={{
          status: "stopped",
          alive: false,
          started: false,
          error: null,
        }}
        ensuringAgent
        onEnsureAgent={vi.fn()}
      />,
    );

    expect(await screen.findByText("Keep this visible.")).toBeVisible();
    expect(screen.getByText("Restarting chat…")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Restarting…" }),
    ).toBeDisabled();
  });

  it("disables structured answers while the document agent needs recovery", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(
        conversation([
          {
            message_id: "question-1",
            role: "agent",
            content: "Continue?",
            message_type: "question",
            status: "pending",
            response_type: "boolean",
          },
        ]),
      ),
    ) as unknown as typeof fetch;

    render(
      <CoworkChatPanel
        provider={provider(fetchImpl)}
        conversationId="c1"
        agent={{
          status: "stopped",
          alive: false,
          started: false,
          error: null,
        }}
        onEnsureAgent={vi.fn()}
      />,
    );

    expect(await screen.findByText("Continue?")).toBeVisible();
    expect(screen.getByRole("button", { name: "Yes" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "No" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Restart chat" })).toBeEnabled();
  });

  it("keeps the transcript visible when a restart request fails", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(
        conversation([
          { message_id: "u1", role: "user", content: "Do not hide this." },
        ]),
      ),
    ) as unknown as typeof fetch;

    render(
      <CoworkChatPanel
        provider={provider(fetchImpl)}
        conversationId="c1"
        agent={{
          status: "stopped",
          alive: false,
          started: false,
          error: null,
        }}
        ensureError="The network is unavailable."
        onEnsureAgent={vi.fn()}
      />,
    );

    expect(await screen.findByText("Do not hide this.")).toBeVisible();
    expect(screen.getByText("The network is unavailable.")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Restart chat" }),
    ).toBeVisible();
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

    await screen.findByText(/Chat couldn’t load/);
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
