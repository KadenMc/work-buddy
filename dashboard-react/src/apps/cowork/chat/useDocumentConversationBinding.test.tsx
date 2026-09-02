import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ChatExecutionSnapshot } from "../../../widget-library/chat";
import type { FeedbackCapture } from "./contracts";
import {
  CoworkDocumentConversationBindingError,
  type CoworkDocumentConversationBinding,
  type CoworkDocumentConversationBindingClient,
} from "./documentConversationBinding";
import { useDocumentConversationBinding } from "./useDocumentConversationBinding";

const runningBinding = (
  conversationId: string,
): CoworkDocumentConversationBinding => ({
  conversationId,
  created: false,
  agent: {
    status: "running",
    alive: true,
    started: false,
    error: null,
  },
  feedback: [],
});

const execution = (
  providerId: string,
  modelId: string,
  revision: string,
): ChatExecutionSnapshot => ({
  selection: {
    providerId,
    modelId,
    providerLabel: providerId === "codex" ? "Codex" : "Claude Code",
    modelLabel: modelId === "gpt-5.6" ? "GPT-5.6" : "Sonnet",
    revision,
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
});

describe("useDocumentConversationBinding", () => {
  it("performs no binding request when Chat is absent from the document capabilities", async () => {
    const client: CoworkDocumentConversationBindingClient = {
      load: vi.fn(async () => runningBinding("must-not-load")),
      ensure: vi.fn(async () => runningBinding("must-not-ensure")),
    };
    const { result } = renderHook(() =>
      useDocumentConversationBinding({
        documentId: "doc-1",
        storeId: "store-1",
        client,
        enabled: false,
      }),
    );

    await act(() => result.current.ensure());
    expect(result.current.phase).toBe("idle");
    expect(client.load).not.toHaveBeenCalled();
    expect(client.ensure).not.toHaveBeenCalled();
  });

  it("restores the existing opaque binding on mount without ensuring", async () => {
    const client: CoworkDocumentConversationBindingClient = {
      load: vi.fn(async () => runningBinding("opaque-existing-91")),
      ensure: vi.fn(async () => runningBinding("should-not-run")),
    };
    const { result } = renderHook(() =>
      useDocumentConversationBinding({
        documentId: "doc-1",
        storeId: "store-1",
        client,
      }),
    );

    await waitFor(() => expect(result.current.phase).toBe("ready"));
    expect(result.current.conversationId).toBe("opaque-existing-91");
    expect(client.load).toHaveBeenCalledOnce();
    expect(client.ensure).not.toHaveBeenCalled();
  });

  it("ensures only after an explicit action and adopts its returned id", async () => {
    const client: CoworkDocumentConversationBindingClient = {
      load: vi.fn(async () => ({
        ...runningBinding("unused"),
        conversationId: null,
        agent: {
          status: "not_started" as const,
          alive: null,
          started: false,
          error: null,
        },
      })),
      ensure: vi.fn(async () => runningBinding("opaque-started-28")),
    };
    const { result } = renderHook(() =>
      useDocumentConversationBinding({
        documentId: "doc-1",
        storeId: "store-1",
        client,
      }),
    );
    await waitFor(() => expect(result.current.phase).toBe("idle"));

    await act(() => result.current.ensure());

    expect(client.ensure).toHaveBeenCalledWith("doc-1", "store-1");
    expect(result.current.phase).toBe("ready");
    expect(result.current.conversationId).toBe("opaque-started-28");
  });

  it("keeps an existing transcript mounted during a repeated preparation", async () => {
    let resolvePreparation!: (
      value: CoworkDocumentConversationBinding,
    ) => void;
    const preparation = new Promise<CoworkDocumentConversationBinding>(
      (resolve) => {
        resolvePreparation = resolve;
      },
    );
    const existing = {
      ...runningBinding("opaque-stopped-12"),
      agent: {
        status: "stopped" as const,
        alive: false,
        started: false,
        error: null,
      },
    };
    const client: CoworkDocumentConversationBindingClient = {
      load: vi.fn(async () => existing),
      ensure: vi.fn(() => preparation),
    };
    const { result } = renderHook(() =>
      useDocumentConversationBinding({
        documentId: "doc-1",
        storeId: "store-1",
        client,
      }),
    );
    await waitFor(() => expect(result.current.phase).toBe("ready"));

    let pending!: Promise<void>;
    act(() => {
      pending = result.current.ensure();
    });
    expect(result.current.phase).toBe("ready");
    expect(result.current.conversationId).toBe("opaque-stopped-12");
    expect(result.current.ensuring).toBe(true);

    resolvePreparation(runningBinding("opaque-stopped-12"));
    await act(async () => pending);
    expect(result.current.phase).toBe("ready");
    expect(result.current.ensuring).toBe(false);
  });

  it("keeps an existing transcript bound when preparation transport fails", async () => {
    const existing = {
      ...runningBinding("opaque-stopped-13"),
      agent: {
        status: "stopped" as const,
        alive: false,
        started: false,
        error: null,
      },
    };
    const client: CoworkDocumentConversationBindingClient = {
      load: vi.fn(async () => existing),
      ensure: vi.fn(async () => {
        throw new Error("The network is unavailable.");
      }),
    };
    const { result } = renderHook(() =>
      useDocumentConversationBinding({
        documentId: "doc-1",
        storeId: "store-1",
        client,
      }),
    );
    await waitFor(() => expect(result.current.phase).toBe("ready"));

    await act(() => result.current.ensure());

    expect(result.current.phase).toBe("ready");
    expect(result.current.conversationId).toBe("opaque-stopped-13");
    expect(result.current.ensuring).toBe(false);
    expect(result.current.error).toBe("The network is unavailable.");
  });

  it("adopts newer execution authority from a failed preparation", async () => {
    const currentExecution = execution(
      "claude-code",
      "sonnet",
      "revision:stale",
    );
    const authoritative = execution(
      "codex",
      "gpt-5.6",
      "revision:new",
    );
    const client: CoworkDocumentConversationBindingClient = {
      load: vi.fn(async () => ({
        ...runningBinding("unused"),
        conversationId: null,
        execution: currentExecution,
      })),
      ensure: vi.fn(async () => {
        throw new CoworkDocumentConversationBindingError(
          "The model choice changed elsewhere.",
          authoritative,
        );
      }),
    };
    const { result } = renderHook(() =>
      useDocumentConversationBinding({
        documentId: "doc-1",
        storeId: "store-1",
        client,
      }),
    );
    await waitFor(() => expect(result.current.phase).toBe("idle"));

    await act(() => result.current.ensure());

    expect(result.current.execution?.selection).toMatchObject({
      providerId: "codex",
      modelId: "gpt-5.6",
      revision: "revision:new",
    });
    expect(result.current.error).toBe(
      "The model choice changed elsewhere.",
    );
  });

  it("adopts R9's real id and rejects a cross-conversation rebind", async () => {
    const client: CoworkDocumentConversationBindingClient = {
      load: vi.fn(async () => ({
        ...runningBinding("unused"),
        conversationId: null,
      })),
      ensure: vi.fn(async () => runningBinding("unused")),
    };
    const { result } = renderHook(() =>
      useDocumentConversationBinding({
        documentId: "doc-1",
        storeId: "store-1",
        client,
      }),
    );
    await waitFor(() => expect(result.current.phase).toBe("idle"));
    const capture: FeedbackCapture = {
      documentId: "doc-1",
      storeId: "store-1",
      evidenceId: "ev-1",
      spanId: "span-1",
      conversationId: "opaque-feedback-54",
      messageId: "feedback-message-1",
      agent: runningBinding("ignored").agent,
      execution: execution(
        "codex",
        "gpt-5.6",
        "revision:feedback",
      ),
      text: "Tighten this.",
    };

    act(() => result.current.adoptFeedback(capture));
    expect(result.current.conversationId).toBe("opaque-feedback-54");
    expect(result.current.execution?.selection).toMatchObject({
      providerId: "codex",
      modelId: "gpt-5.6",
      revision: "revision:feedback",
    });

    act(() =>
      result.current.adoptFeedback({
        ...capture,
        conversationId: "different-conversation",
      }),
    );
    expect(result.current.conversationId).toBe("opaque-feedback-54");
    expect(result.current.phase).toBe("error");
    expect(result.current.error).toMatch(/changed unexpectedly/i);
  });

  it("rejects an execution envelope from the previously open document", async () => {
    const client: CoworkDocumentConversationBindingClient = {
      load: vi.fn(async (documentId) => ({
        ...runningBinding(`conversation:${documentId}`),
        execution: execution(
          "claude-code",
          "sonnet",
          `revision:${documentId}`,
        ),
      })),
      ensure: vi.fn(),
    };
    const { result, rerender } = renderHook(
      ({
        documentId,
        storeId,
      }: {
        documentId: string;
        storeId: string;
      }) =>
        useDocumentConversationBinding({
          documentId,
          storeId,
          client,
        }),
      {
        initialProps: {
          documentId: "doc-old",
          storeId: "store-old",
        },
      },
    );
    await waitFor(() =>
      expect(result.current.execution?.selection.revision).toBe(
        "revision:doc-old",
      ),
    );

    rerender({ documentId: "doc-new", storeId: "store-new" });
    await waitFor(() =>
      expect(result.current.execution?.selection.revision).toBe(
        "revision:doc-new",
      ),
    );

    act(() =>
      result.current.adoptExecution(
        "doc-old",
        "store-old",
        execution("codex", "gpt-5.6", "revision:late-old"),
      ),
    );
    expect(result.current.execution?.selection).toMatchObject({
      providerId: "claude-code",
      modelId: "sonnet",
      revision: "revision:doc-new",
    });
  });

  it("does not let a slow initial load overwrite a sitting-adopted binding", async () => {
    let resolveLoad!: (
      value: CoworkDocumentConversationBinding,
    ) => void;
    const pendingLoad = new Promise<CoworkDocumentConversationBinding>(
      (resolve) => {
        resolveLoad = resolve;
      },
    );
    const client: CoworkDocumentConversationBindingClient = {
      load: vi.fn(() => pendingLoad),
      ensure: vi.fn(),
    };
    const { result } = renderHook(() =>
      useDocumentConversationBinding({
        documentId: "doc-1",
        storeId: "store-1",
        client,
      }),
    );

    act(() =>
      result.current.adoptExecution(
        "doc-1",
        "store-1",
        execution("codex", "gpt-5.6", "revision:sitting"),
        runningBinding("ignored").agent,
        "conversation-from-sitting",
      ),
    );
    expect(result.current.conversationId).toBe("conversation-from-sitting");

    await act(async () => {
      resolveLoad({
        ...runningBinding("unused"),
        conversationId: null,
        execution: execution(
          "claude-code",
          "sonnet",
          "revision:old-load",
        ),
      });
      await pendingLoad;
    });

    expect(result.current.conversationId).toBe("conversation-from-sitting");
    expect(result.current.execution?.selection).toMatchObject({
      providerId: "codex",
      modelId: "gpt-5.6",
      revision: "revision:sitting",
    });
  });

  it("does not let a superseded ensure clear a newer retry", async () => {
    let resolveFirst!: (
      value: CoworkDocumentConversationBinding,
    ) => void;
    let resolveSecond!: (
      value: CoworkDocumentConversationBinding,
    ) => void;
    const firstEnsure = new Promise<CoworkDocumentConversationBinding>(
      (resolve) => {
        resolveFirst = resolve;
      },
    );
    const secondEnsure = new Promise<CoworkDocumentConversationBinding>(
      (resolve) => {
        resolveSecond = resolve;
      },
    );
    const client: CoworkDocumentConversationBindingClient = {
      load: vi.fn(async () => ({
        ...runningBinding("unused"),
        conversationId: null,
      })),
      ensure: vi
        .fn()
        .mockImplementationOnce(() => firstEnsure)
        .mockImplementationOnce(() => secondEnsure),
    };
    const { result } = renderHook(() =>
      useDocumentConversationBinding({
        documentId: "doc-1",
        storeId: "store-1",
        client,
      }),
    );
    await waitFor(() => expect(result.current.phase).toBe("idle"));

    let first!: Promise<void>;
    act(() => {
      first = result.current.ensure();
    });
    act(() =>
      result.current.adoptFeedback({
        documentId: "doc-1",
        storeId: "store-1",
        evidenceId: "ev-race",
        spanId: "span-race",
        conversationId: "opaque-feedback",
        messageId: "message-race",
        agent: {
          status: "spawn_failed",
          alive: false,
          started: false,
          error: "driver unavailable",
        },
        text: "Keep this feedback.",
      }),
    );

    let second!: Promise<void>;
    act(() => {
      second = result.current.ensure();
    });
    resolveFirst(runningBinding("superseded-chat"));
    await act(async () => first);

    let duplicate!: Promise<void>;
    act(() => {
      duplicate = result.current.ensure();
    });
    expect(duplicate).toBe(second);
    expect(client.ensure).toHaveBeenCalledTimes(2);

    resolveSecond(runningBinding("opaque-feedback"));
    await act(async () => second);
    expect(result.current.phase).toBe("ready");
    expect(result.current.conversationId).toBe("opaque-feedback");
  });

  it("ignores a late binding response after switching documents", async () => {
    let resolveOld!: (value: CoworkDocumentConversationBinding) => void;
    const old = new Promise<CoworkDocumentConversationBinding>((resolve) => {
      resolveOld = resolve;
    });
    const client: CoworkDocumentConversationBindingClient = {
      load: vi.fn((documentId: string) =>
        documentId === "old-doc"
          ? old
          : Promise.resolve(runningBinding("new-conversation")),
      ),
      ensure: vi.fn(async () => runningBinding("unused")),
    };
    const { result, rerender } = renderHook(
      ({ documentId }) =>
        useDocumentConversationBinding({
          documentId,
          storeId: "store-1",
          client,
        }),
      { initialProps: { documentId: "old-doc" } },
    );

    rerender({ documentId: "new-doc" });
    await waitFor(() =>
      expect(result.current.conversationId).toBe("new-conversation"),
    );
    act(() => resolveOld(runningBinding("old-conversation")));
    await act(async () => Promise.resolve());

    expect(result.current.conversationId).toBe("new-conversation");
  });

  it("does not let an old document's feedback callback invalidate the new load", async () => {
    let resolveNew!: (value: CoworkDocumentConversationBinding) => void;
    const newLoad = new Promise<CoworkDocumentConversationBinding>((resolve) => {
      resolveNew = resolve;
    });
    const client: CoworkDocumentConversationBindingClient = {
      load: vi.fn((documentId: string) =>
        documentId === "old-doc"
          ? Promise.resolve(runningBinding("old-conversation"))
          : newLoad,
      ),
      ensure: vi.fn(async () => runningBinding("unused")),
    };
    const { result, rerender } = renderHook(
      ({ documentId }) =>
        useDocumentConversationBinding({
          documentId,
          storeId: "store-1",
          client,
        }),
      { initialProps: { documentId: "old-doc" } },
    );
    await waitFor(() =>
      expect(result.current.conversationId).toBe("old-conversation"),
    );
    const adoptOldFeedback = result.current.adoptFeedback;

    rerender({ documentId: "new-doc" });
    act(() =>
      adoptOldFeedback({
        documentId: "old-doc",
        storeId: "store-1",
        evidenceId: "old-evidence",
        spanId: "old-span",
        conversationId: "old-conversation",
        messageId: "old-message",
        agent: runningBinding("ignored").agent,
        text: "Late feedback from the old document.",
      }),
    );

    resolveNew(runningBinding("new-conversation"));
    await waitFor(() =>
      expect(result.current.conversationId).toBe("new-conversation"),
    );
    expect(result.current.phase).toBe("ready");
  });
});
