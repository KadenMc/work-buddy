import { describe, expect, it, vi } from "vitest";

import {
  CoworkHttpClient,
  normalizeDocumentSummary,
} from "./CoworkHttpClient";

const json = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });

describe("CoworkHttpClient document lifecycle contracts", () => {
  it("falls back to the path basename when the server title is blank", () => {
    expect(
      normalizeDocumentSummary({
        document_id: "doc-1",
        path: "Docs/My Working Note.MD",
        title: "   ",
      }).title,
    ).toBe("My Working Note.MD");
  });

  it("uses prepare, exact staged source, and multipart commit for re-import", async () => {
    const requests: Array<{ readonly url: string; readonly init: RequestInit }> = [];
    const source = new TextEncoder().encode("# External source\n");
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = String(input);
      requests.push({ url, init });
      if (url.endsWith("/reimport?store_id=store-1")) {
        expect(init.method).toBe("POST");
        expect(init.credentials).toBe("same-origin");
        expect(JSON.parse(String(init.body))).toEqual({ idempotency_key: "stable-key" });
        return json({
          intent_id: "reimport-1",
          state: "prepared",
          expires_at: "2026-07-22T20:00:00Z",
          source_sha256: "a".repeat(64),
          source_byte_length: source.byteLength,
          prior_projection_sha256: "b".repeat(64),
          prior_snapshot_sha256: "c".repeat(64),
          prior_structured_head_sha256: "d".repeat(64),
          consequence: "Replace structured state from the staged Markdown.",
        });
      }
      if (url.endsWith("/reimport/reimport-1/source?store_id=store-1")) {
        expect(init).toMatchObject({ method: "GET", credentials: "same-origin" });
        return new Response(source, { status: 200 });
      }
      if (url.endsWith("/reimport/reimport-1/commit?store_id=store-1")) {
        expect(init.method).toBe("PUT");
        expect(init.credentials).toBe("same-origin");
        const form = init.body as FormData;
        const metadata = form.get("metadata");
        const snapshot = form.get("snapshot");
        expect(typeof metadata).toBe("string");
        expect(snapshot).toBeInstanceOf(Blob);
        expect(JSON.parse(String(metadata))).toEqual({
          snapshot_sha256: "e".repeat(64),
        });
        return json({
          intent_id: "reimport-1",
          document_id: "doc-1",
          source_sha256: "a".repeat(64),
          snapshot_sha256: "e".repeat(64),
          structured_head_sha256: "f".repeat(64),
          document_version_id: "version-2",
          doc_event_id: "event-2",
          staled_proposal_ids: ["proposal-1"],
          reimported_at: "2026-07-22T19:00:00Z",
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const client = new CoworkHttpClient(fetchImpl as typeof fetch);

    const prepared = await client.prepareReimport("store-1", "doc-1", "stable-key");
    expect(prepared).toMatchObject({ intentId: "reimport-1", state: "prepared" });
    expect(
      Array.from(
        await client.readReimportSource("store-1", "doc-1", prepared.intentId),
      ),
    ).toEqual(Array.from(source));
    const receipt = await client.commitReimport(
      "store-1",
      "doc-1",
      prepared,
      new Uint8Array([1, 2, 3]),
      "e".repeat(64),
    );
    expect(receipt).toMatchObject({
      documentId: "doc-1",
      documentVersionId: "version-2",
      staledProposalIds: ["proposal-1"],
    });
    expect(requests).toHaveLength(3);
  });

  it("uses the same retirement route for server-prepared consequence and commit", async () => {
    const bodies: unknown[] = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      expect(String(input)).toBe("/api/truth/doc/doc-1/retire?store_id=store-1");
      expect(init).toMatchObject({ method: "POST", credentials: "same-origin" });
      const body = JSON.parse(String(init.body));
      bodies.push(body);
      if ("idempotency_key" in body) {
        return json({
          intent_id: "retire-1",
          expires_at: "2026-07-22T20:00:00Z",
          document_id: "doc-1",
          consequence: "The Markdown file and history are retained.",
          consequence_sha256: "a".repeat(64),
        });
      }
      return json({
        intent_id: "retire-1",
        document_id: "doc-1",
        lifecycle: "retired",
        retired_at: "2026-07-22T19:00:00Z",
        doc_event_id: "event-retired",
        file_retained: true,
        history_retained: true,
      });
    });
    const client = new CoworkHttpClient(fetchImpl as typeof fetch);

    const prepared = await client.prepareRetirement("store-1", "doc-1", "retire-key");
    expect(prepared.consequence).toContain("file and history are retained");
    const receipt = await client.commitRetirement(
      "store-1",
      "doc-1",
      prepared.intentId,
    );
    expect(receipt).toMatchObject({
      lifecycle: "retired",
      fileRetained: true,
      historyRetained: true,
    });
    expect(bodies).toEqual([
      { idempotency_key: "retire-key" },
      { intent_id: "retire-1" },
    ]);
  });
});
