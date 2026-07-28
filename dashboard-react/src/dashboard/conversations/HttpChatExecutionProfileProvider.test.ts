import { describe, expect, it, vi } from "vitest";

import { ChatExecutionSelectionError } from "../../widget-library/chat";
import {
  HttpChatExecutionProfileProvider,
  normalizeChatExecutionEnvelope,
} from "./HttpChatExecutionProfileProvider";

const jsonResponse = (body: unknown, status = 200): Response =>
  ({
    ok: status >= 200 && status < 300,
    status,
    statusText: "",
    json: async () => body,
  }) as unknown as Response;

const deferred = <T,>() => {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
};

const executionPayload = (
  providerId = "claude-code",
  modelId = "sonnet",
  revision = "",
) => ({
  execution: {
    selection: {
      provider_id: providerId,
      model_id: modelId,
      provider_label: providerId === "codex" ? "Codex" : "Claude Code",
      model_label: modelId === "gpt-5.6" ? "GPT-5.6" : "Sonnet",
      revision,
    },
    providers: [
      {
        id: "claude-code",
        label: "Claude Code",
        available: true,
        availability: "ready",
        auth_mode: "subscription",
        models: [{ id: "sonnet", label: "Sonnet", available: true }],
      },
      {
        id: "codex",
        label: "Codex",
        available: false,
        availability: "auth_required",
        unavailable_reason: "Sign in to Codex",
        auth_mode: "chatgpt",
        models: [
          {
            id: "gpt-5.6",
            label: "GPT-5.6",
            available: false,
            unavailable_reason: "Codex is not signed in",
          },
        ],
      },
    ],
    read_only: false,
  },
  agent: {
    status: "not_started",
    alive: null,
    started: false,
    error: null,
  },
});

describe("HttpChatExecutionProfileProvider", () => {
  it("normalizes the server catalog, projected revision, and host envelope", () => {
    const envelope = normalizeChatExecutionEnvelope(executionPayload());

    expect(envelope.execution).toMatchObject({
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
          authMode: "subscription",
          available: true,
        },
        {
          id: "codex",
          authMode: "chatgpt",
          available: false,
          unavailableReason: "Sign in to Codex",
          models: [
            {
              id: "gpt-5.6",
              available: false,
              unavailableReason: "Codex is not signed in",
            },
          ],
        },
      ],
    });
    expect(envelope.agent).toMatchObject({ status: "not_started" });
  });

  it("PATCHes the exact atomic pair and optimistic revision", async () => {
    const onEnvelope = vi.fn();
    const fetchImpl = vi.fn(async () =>
      jsonResponse(executionPayload("codex", "gpt-5.6", "execution:2")),
    );
    const provider = new HttpChatExecutionProfileProvider({
      targetId: "store\u0000doc",
      loadUrl: "/load",
      selectUrl: "/select",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onEnvelope,
    });

    const result = await provider.select("store\u0000doc", {
      providerId: "codex",
      modelId: "gpt-5.6",
      expectedRevision: "execution:1",
    });

    expect(fetchImpl).toHaveBeenCalledWith("/select", {
      method: "PATCH",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        provider_id: "codex",
        model_id: "gpt-5.6",
        expected_revision: "execution:1",
      }),
    });
    expect(result.selection).toMatchObject({
      providerId: "codex",
      modelId: "gpt-5.6",
      revision: "execution:2",
    });
    expect(onEnvelope).toHaveBeenCalledWith(
      expect.objectContaining({ execution: result }),
    );
  });

  it("carries an authoritative conflict snapshot to the state hook", async () => {
    const onEnvelope = vi.fn();
    const fetchImpl = vi.fn(async () =>
      jsonResponse(
        {
          ...executionPayload("codex", "gpt-5.6", "execution:9"),
          error: "The selection changed in another window.",
        },
        409,
      ),
    );
    const provider = new HttpChatExecutionProfileProvider({
      targetId: "document:1",
      loadUrl: "/load",
      selectUrl: "/select",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onEnvelope,
    });

    try {
      await provider.select("document:1", {
        providerId: "codex",
        modelId: "gpt-5.6",
        expectedRevision: "execution:1",
      });
      throw new Error("Expected selection conflict.");
    } catch (error) {
      expect(error).toBeInstanceOf(ChatExecutionSelectionError);
      expect(
        (error as ChatExecutionSelectionError).authoritativeSnapshot?.selection,
      ).toMatchObject({
        providerId: "codex",
        modelId: "gpt-5.6",
        revision: "execution:9",
      });
      expect(onEnvelope).toHaveBeenCalledWith(
        expect.objectContaining({
          execution: expect.objectContaining({
            selection: expect.objectContaining({ revision: "execution:9" }),
          }),
        }),
      );
    }
  });

  it("reloads an authoritative snapshot when a conflict has no envelope", async () => {
    const onEnvelope = vi.fn();
    const fetchImpl = vi.fn(
      async (_input: RequestInfo | URL, init?: RequestInit) =>
        init?.method === "PATCH"
          ? jsonResponse(
              { ok: false, error: "The selection changed elsewhere." },
              409,
            )
          : jsonResponse(
              executionPayload("codex", "gpt-5.6", "execution:11"),
            ),
    );
    const provider = new HttpChatExecutionProfileProvider({
      targetId: "document:1",
      loadUrl: "/load",
      selectUrl: "/select",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onEnvelope,
    });

    await expect(
      provider.select("document:1", {
        providerId: "codex",
        modelId: "gpt-5.6",
        expectedRevision: "execution:1",
      }),
    ).rejects.toMatchObject({
      authoritativeSnapshot: expect.objectContaining({
        selection: expect.objectContaining({ revision: "execution:11" }),
      }),
    });
    expect(fetchImpl).toHaveBeenNthCalledWith(2, "/load", {
      headers: { Accept: "application/json" },
    });
    expect(onEnvelope).toHaveBeenCalledOnce();
  });

  it("notifies a mounted hook when a containing feature adopts a newer snapshot", () => {
    const initial = normalizeChatExecutionEnvelope(
      executionPayload(),
    ).execution;
    const pinned = normalizeChatExecutionEnvelope(
      executionPayload("claude-code", "sonnet", "execution:pinned"),
    ).execution;
    const provider = new HttpChatExecutionProfileProvider({
      targetId: "document:1",
      loadUrl: "/load",
      selectUrl: "/select",
      initialSnapshot: initial,
      fetchImpl: vi.fn() as unknown as typeof fetch,
    });
    const invalidate = vi.fn();
    provider.subscribe("document:1", invalidate);

    provider.replaceSnapshot(initial);
    expect(invalidate).not.toHaveBeenCalled();
    provider.replaceSnapshot(pinned);
    expect(invalidate).toHaveBeenCalledOnce();
  });

  it("does not adopt a late GET after the host replaces the snapshot", async () => {
    const pendingLoad = deferred<Response>();
    const onEnvelope = vi.fn();
    const provider = new HttpChatExecutionProfileProvider({
      targetId: "document:1",
      loadUrl: "/load",
      selectUrl: "/select",
      fetchImpl: vi.fn(() => pendingLoad.promise) as unknown as typeof fetch,
      onEnvelope,
    });
    const replacement = normalizeChatExecutionEnvelope(
      executionPayload("codex", "gpt-5.6", "execution:host"),
    ).execution;

    const load = provider.load("document:1");
    provider.replaceSnapshot(replacement);
    pendingLoad.resolve(
      jsonResponse(
        executionPayload("claude-code", "sonnet", "execution:stale"),
      ),
    );

    await expect(load).resolves.toBe(replacement);
    await expect(provider.load("document:1")).resolves.toBe(replacement);
    expect(onEnvelope).not.toHaveBeenCalled();
  });

  it("lets a successful PATCH supersede an older in-flight GET", async () => {
    const pendingLoad = deferred<Response>();
    const pendingPatch = deferred<Response>();
    const onEnvelope = vi.fn();
    const fetchImpl = vi.fn(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        init?.method === "PATCH"
          ? pendingPatch.promise
          : pendingLoad.promise,
    );
    const provider = new HttpChatExecutionProfileProvider({
      targetId: "document:1",
      loadUrl: "/load",
      selectUrl: "/select",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onEnvelope,
    });

    const load = provider.load("document:1");
    const select = provider.select("document:1", {
      providerId: "codex",
      modelId: "gpt-5.6",
      expectedRevision: "execution:1",
    });
    pendingPatch.resolve(
      jsonResponse(executionPayload("codex", "gpt-5.6", "execution:2")),
    );
    const selected = await select;
    pendingLoad.resolve(
      jsonResponse(
        executionPayload("claude-code", "sonnet", "execution:stale"),
      ),
    );

    await expect(load).resolves.toBe(selected);
    expect(selected.selection).toMatchObject({
      providerId: "codex",
      modelId: "gpt-5.6",
      revision: "execution:2",
    });
    expect(onEnvelope).toHaveBeenCalledOnce();
    expect(onEnvelope).toHaveBeenCalledWith(
      expect.objectContaining({ execution: selected }),
    );
  });

  it("drops its cached projection before an explicit refresh", async () => {
    const initial = normalizeChatExecutionEnvelope(
      executionPayload(),
    ).execution;
    const refreshed = executionPayload(
      "claude-code",
      "sonnet",
      "execution:refreshed",
    );
    const fetchImpl = vi.fn(async () => jsonResponse(refreshed));
    const provider = new HttpChatExecutionProfileProvider({
      targetId: "document:1",
      loadUrl: "/load",
      selectUrl: "/select",
      initialSnapshot: initial,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    expect(await provider.load("document:1")).toBe(initial);
    provider.refresh("document:1");
    expect((await provider.load("document:1")).selection.revision).toBe(
      "execution:refreshed",
    );
    expect(fetchImpl).toHaveBeenCalledWith(
      "/load?refresh_execution=1",
      { headers: { Accept: "application/json" } },
    );
  });

  it("rejects a malformed execution response", () => {
    expect(() =>
      normalizeChatExecutionEnvelope({
        execution: { selection: {}, providers: [] },
      }),
    ).toThrow(/provider_id/);
  });
});
