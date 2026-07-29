import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  ChatExecutionControl,
  ChatExecutionSnapshot,
} from "../../../widget-library/chat";
import { useCoworkVerifyExecution } from "./useCoworkVerifyExecution";

const snapshot = (
  providerId = "claude-code",
  modelId = "sonnet",
): ChatExecutionSnapshot => ({
  selection: {
    providerId,
    modelId,
    providerLabel: providerId === "codex" ? "Codex" : "Claude Code",
    modelLabel: modelId === "gpt-5.6-sol" ? "GPT-5.6" : "Sonnet",
    revision: "3",
  },
  providers: [
    {
      id: "claude-code",
      label: "Claude Code",
      available: true,
      models: [
        {
          id: "sonnet",
          label: "Sonnet",
          available: true,
        },
      ],
    },
    {
      id: "codex",
      label: "Codex",
      available: true,
      models: [
        {
          id: "gpt-5.6-sol",
          label: "GPT-5.6",
          available: true,
        },
      ],
    },
  ],
  readOnly: false,
});

const control = (value: ChatExecutionSnapshot): ChatExecutionControl => ({
  snapshot: value,
  status: "ready",
  selecting: false,
  error: null,
  announcement: null,
  currentAvailable: true,
  select: vi.fn(async () => undefined),
  retry: vi.fn(),
});

describe("useCoworkVerifyExecution", () => {
  it("changes Verify locally without mutating Chat", async () => {
    const chat = control(snapshot());
    const { result } = renderHook(() =>
      useCoworkVerifyExecution(chat, "store\u0000doc"),
    );
    await waitFor(() =>
      expect(result.current?.snapshot?.selection.providerId).toBe(
        "claude-code",
      ),
    );

    await act(async () => {
      await result.current?.select("codex", "gpt-5.6-sol");
    });

    expect(result.current?.snapshot?.selection).toMatchObject({
      providerId: "codex",
      modelId: "gpt-5.6-sol",
      providerLabel: "Codex",
      modelLabel: "GPT-5.6",
    });
    expect(chat.select).not.toHaveBeenCalled();
  });

  it("does not silently follow a later Chat selection", async () => {
    const initial = control(snapshot());
    const { result, rerender } = renderHook(
      ({ source }) =>
        useCoworkVerifyExecution(source, "store\u0000doc"),
      { initialProps: { source: initial } },
    );
    await waitFor(() =>
      expect(result.current?.snapshot?.selection.providerId).toBe(
        "claude-code",
      ),
    );

    rerender({ source: control(snapshot("codex", "gpt-5.6-sol")) });

    expect(result.current?.snapshot?.selection).toMatchObject({
      providerId: "claude-code",
      modelId: "sonnet",
    });
  });

  it("resets to the source default for another document", async () => {
    const source = control(snapshot());
    const { result, rerender } = renderHook(
      ({ identity }) => useCoworkVerifyExecution(source, identity),
      { initialProps: { identity: "store\u0000doc-a" } },
    );
    await act(async () => {
      await result.current?.select("codex", "gpt-5.6-sol");
    });
    rerender({ identity: "store\u0000doc-b" });

    await waitFor(() =>
      expect(result.current?.snapshot?.selection.providerId).toBe(
        "claude-code",
      ),
    );
  });
});
