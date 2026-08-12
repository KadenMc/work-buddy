import { describe, expect, it, vi } from "vitest";

vi.mock("../../../security/humanAuthority", () => ({
  coworkHumanAuthorityHeaders: vi.fn(async () => ({})),
}));

import { HttpCoworkMaterializationClient } from "./HttpCoworkMaterializationClient";

const request = {
  renderedMarkdown: "# Saved\n",
  renderedSha256: "a".repeat(64),
  expectedFileSha256: "b".repeat(64),
  expectedStructuredHeadSha256: "c".repeat(64),
  snapshotSha256: "d".repeat(64),
  idempotencyKey: "save-once",
};

describe("HttpCoworkMaterializationClient", () => {
  it("posts the canonical Markdown with both file and structured CAS preconditions", async () => {
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      void init;
      return new Response(
        JSON.stringify({
          ok: true,
          new_file_sha256: request.renderedSha256,
          structured_head_sha256: request.expectedStructuredHeadSha256,
          document_version_id: "version-1",
          materialized_at: "2026-07-22T12:00:00.000Z",
          drift_state: "clean",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });
    const client = new HttpCoworkMaterializationClient(fetchImpl as typeof fetch);

    const receipt = await client.materialize("store/one", "doc one", request);

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(String(url)).toBe(
      "/api/truth/doc/doc%20one/materialize?store_id=store%2Fone",
    );
    expect(JSON.parse(String(init?.body))).toEqual({
      rendered_markdown: "# Saved\n",
      rendered_sha256: request.renderedSha256,
      expected_file_sha256: request.expectedFileSha256,
      expected_ydoc_head_sha256: request.expectedStructuredHeadSha256,
      snapshot_sha256: request.snapshotSha256,
      idempotency_key: "save-once",
    });
    expect(receipt).toMatchObject({
      newFileSha256: request.renderedSha256,
      driftState: "clean",
    });
  });

  it("preserves typed dual-CAS conflict details for safe UI recovery", async () => {
    const fetchImpl = vi.fn(async () =>
      new Response(
        JSON.stringify({
          ok: false,
          error: {
            code: "stale_file",
            message: "Markdown file changed outside Co-work",
            retryable: false,
            details: { current_file_sha256: "e".repeat(64) },
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );
    const client = new HttpCoworkMaterializationClient(fetchImpl as typeof fetch);

    await expect(client.materialize("store", "doc", request)).rejects.toMatchObject({
      apiError: {
        code: "stale_file",
        status: 409,
        retryable: false,
        details: { current_file_sha256: "e".repeat(64) },
      },
    });
  });
});
