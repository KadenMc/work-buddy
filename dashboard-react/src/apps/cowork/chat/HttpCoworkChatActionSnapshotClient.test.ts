import { describe, expect, it, vi } from "vitest";

vi.mock("../../../security/humanAuthority", () => ({
  coworkHumanAuthorityHeaders: vi.fn(async () => ({})),
}));

import type { CoworkCapturedActionSnapshot } from "../targets";
import { HttpCoworkChatActionSnapshotClient } from "./HttpCoworkChatActionSnapshotClient";

const response = (
  payload: unknown,
  status = 201,
): Response =>
  ({
    ok: status >= 200 && status < 300,
    status,
    statusText: "",
    json: async () => payload,
  }) as Response;

const capture = {
  schema: "wb.cowork.action-snapshot/v1",
  storeId: "store-1",
  documentId: "doc-1",
  captureId: "capture-1",
} as CoworkCapturedActionSnapshot;

describe("HttpCoworkChatActionSnapshotClient", () => {
  it("persists the exact capture and normalizes its durable reference", async () => {
    const fetchImpl = vi.fn(async () =>
      response({
        ok: true,
        context: {
          kind: "action_snapshot",
          action_snapshot_id: "action-1",
          store_id: "store-1",
          document_id: "doc-1",
          target_kind: "text_quote",
          target_label: "Introduction",
          target_word_count: 24,
          target_text_sha256: "a".repeat(64),
          projection_sha256: "b".repeat(64),
          captured_at: "2026-07-28T12:00:00Z",
        },
      }),
    ) as unknown as typeof fetch;
    const client = new HttpCoworkChatActionSnapshotClient({
      storeId: "store-1",
      documentId: "doc-1",
      fetchImpl,
    });

    const context = await client.prepare(capture);

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/truth/doc/doc-1/chat/action-snapshots?store_id=store-1",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ capture }),
      }),
    );
    expect(context).toMatchObject({
      kind: "action_snapshot",
      actionSnapshotId: "action-1",
      targetLabel: "Introduction",
      targetWordCount: 24,
    });
  });

  it("surfaces the server's explicit unavailable message", async () => {
    const fetchImpl = vi.fn(async () =>
      response(
        {
          error:
            "Working-on context is unavailable because the document assistant is not running.",
          code: "action_snapshot_changed",
        },
        409,
      ),
    ) as unknown as typeof fetch;
    const client = new HttpCoworkChatActionSnapshotClient({
      storeId: "store-1",
      documentId: "doc-1",
      fetchImpl,
    });

    const pending = client.prepare(capture);
    await expect(pending).rejects.toThrow("document assistant is not running");
    await expect(pending).rejects.toMatchObject({
      status: 409,
      code: "action_snapshot_changed",
    });
  });
});
