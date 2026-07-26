import { describe, expect, it, vi } from "vitest";

import { HttpCoworkYdocTransport } from "./HttpCoworkYdocTransport";
import { frameSegments, parseFrames } from "./framing";

const bytes = (...values: number[]): Uint8Array => new Uint8Array(values);

interface FakeResponseInit {
  readonly ok?: boolean;
  readonly status?: number;
  readonly headers?: Record<string, string>;
  readonly body?: Uint8Array;
  readonly json?: unknown;
}

const fakeResponse = (init: FakeResponseInit): Response => {
  const headers = init.headers ?? {};
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    headers: { get: (name: string) => headers[name] ?? null },
    arrayBuffer: async () =>
      (init.body ?? new Uint8Array()).buffer as ArrayBuffer,
    json: async () => init.json,
  } as unknown as Response;
};

/** A typed fetch mock so `mock.calls[0]` carries the real request tuple. */
const mockFetch = (respond: (init: RequestInit | undefined) => Response) =>
  vi.fn(
    async (_input: RequestInfo | URL, init?: RequestInit): Promise<Response> =>
      respond(init),
  );

const transportWith = (fetchImpl: ReturnType<typeof mockFetch>) =>
  new HttpCoworkYdocTransport({
    documentId: "d1",
    storeId: "s1",
    fetchImpl: fetchImpl as unknown as typeof fetch,
  });

describe("HttpCoworkYdocTransport", () => {
  it("splits a full pull into its leading snapshot and following batches", async () => {
    const snapshot = bytes(1, 2, 3);
    const batchA = bytes(9, 9);
    const batchB = bytes(7);
    const fetchImpl = mockFetch(() =>
      fakeResponse({
        headers: {
          "X-WB-Snapshot-Sha256": "snap",
          "X-WB-Ydoc-Generation": "generation-1",
          "X-WB-Doc-Sha256": "doc",
          "X-WB-Next-Offset": "3",
        },
        body: frameSegments([snapshot, batchA, batchB]),
      }),
    );

    const pull = await transportWith(fetchImpl).pull({});

    const [url, options] = fetchImpl.mock.calls[0];
    expect(url).toBe("/api/truth/doc/d1/ydoc?store_id=s1");
    expect(options?.method).toBe("GET");
    expect(pull.snapshot).toEqual(snapshot);
    expect(pull.snapshotSha256).toBe("snap");
    expect(pull.ydocGeneration).toBe("generation-1");
    expect(pull.batches).toEqual([batchA, batchB]);
    expect(pull.docSha256).toBe("doc");
    expect(pull.nextOffset).toBe("3");
  });

  it("treats an offset-sliced pull as batches only, no snapshot", async () => {
    const batch = bytes(4, 5, 6);
    const fetchImpl = mockFetch(() =>
      fakeResponse({
        headers: {
          "X-WB-Doc-Sha256": "doc",
          "X-WB-Ydoc-Generation": "generation-1",
          "X-WB-Next-Offset": "9",
        },
        body: frameSegments([batch]),
      }),
    );

    const pull = await transportWith(fetchImpl).pull({ sinceOffset: "8" });

    const [, options] = fetchImpl.mock.calls[0];
    expect(options?.headers).toMatchObject({ "X-WB-Since-Offset": "8" });
    expect(pull.snapshot).toBeNull();
    expect(pull.batches).toEqual([batch]);
  });

  it("recognizes the leading replacement snapshot when an offset cursor resets", async () => {
    const snapshot = bytes(1, 2, 3);
    const batch = bytes(4, 5);
    const fetchImpl = mockFetch(() =>
      fakeResponse({
        headers: {
          "X-WB-Cursor-Reset": "1",
          "X-WB-Snapshot-Sha256": "replacement",
          "X-WB-Ydoc-Generation": "generation-2",
          "X-WB-Ydoc-Head-Sha256": "new-head",
          "X-WB-Next-Offset": "new-generation:1",
        },
        body: frameSegments([snapshot, batch]),
      }),
    );

    const pull = await transportWith(fetchImpl).pull({
      sinceOffset: "old-generation:9",
    });

    expect(pull.cursorReset).toBe(true);
    expect(pull.snapshot).toEqual(snapshot);
    expect(pull.snapshotSha256).toBe("replacement");
    expect(pull.batches).toEqual([batch]);
  });

  it("fails closed when a cursor reset omits its replacement snapshot", async () => {
    const fetchImpl = mockFetch(() =>
      fakeResponse({
        headers: {
          "X-WB-Cursor-Reset": "1",
          "X-WB-Ydoc-Generation": "generation-2",
          "X-WB-Ydoc-Head-Sha256": "new-head",
        },
        body: frameSegments([bytes(4, 5)]),
      }),
    );

    await expect(
      transportWith(fetchImpl).pull({ sinceOffset: "old-generation:9" }),
    ).rejects.toThrow(/omitted its replacement snapshot/);
  });

  it("sends a plain push as the raw batch and reports the applied result", async () => {
    const batch = bytes(1, 1, 2);
    const fetchImpl = mockFetch(() =>
      fakeResponse({
        json: {
          ok: true,
          applied: true,
          doc_sha256: "doc",
          ydoc_generation: "generation-1",
          next_offset: "5",
        },
      }),
    );

    const result = await transportWith(fetchImpl).push({
      batch,
      baseSha256: "base",
      baseYdocGeneration: "generation-1",
    });

    const [, options] = fetchImpl.mock.calls[0];
    expect(options?.method).toBe("POST");
    expect(options?.headers).toMatchObject({
      "X-WB-Base-Sha256": "base",
      "X-WB-Base-Ydoc-Sha256": "base",
      "X-WB-Base-Ydoc-Generation": "generation-1",
    });
    expect(options?.body).toEqual(batch);
    expect(result).toEqual({
      ok: true,
      applied: true,
      docSha256: "doc",
      structuredHeadSha256: "doc",
      ydocGeneration: "generation-1",
      projectionSha256: "",
      nextOffset: "5",
    });
  });

  it("frames a compaction push as batch then snapshot and announces the digest", async () => {
    const batch = bytes(1);
    const snapshot = bytes(2, 2);
    const fetchImpl = mockFetch(() =>
      fakeResponse({
        json: {
          ok: true,
          applied: true,
          doc_sha256: "doc",
          ydoc_generation: "generation-1",
          next_offset: "6",
        },
      }),
    );

    await transportWith(fetchImpl).push({
      batch,
      baseSha256: "base",
      baseYdocGeneration: "generation-1",
      compaction: { snapshot, snapshotSha256: "snap" },
    });

    const [, options] = fetchImpl.mock.calls[0];
    expect(options?.headers).toMatchObject({
      "X-WB-Compacted-Snapshot-Sha256": "snap",
    });
    const framed = options?.body as Uint8Array;
    expect(parseFrames(framed)).toEqual([batch, snapshot]);
  });

  it("maps only an explicit stale_base response into a retry result", async () => {
    const fetchImpl = mockFetch(() =>
      fakeResponse({
        ok: false,
        status: 409,
        json: {
          ok: false,
          error: "stale_base",
          server_doc_sha256: "server",
          server_ydoc_generation: "generation-server",
        },
      }),
    );

    const result = await transportWith(fetchImpl).push({
      batch: bytes(1),
      baseSha256: "old",
      baseYdocGeneration: "generation-1",
    });

    expect(result).toEqual({
      ok: false,
      error: "stale_base",
      serverDocSha256: "server",
      serverStructuredHeadSha256: "server",
      serverYdocGeneration: "generation-server",
    });
  });

  it("propagates a terminal lifecycle gate instead of retrying it as stale", async () => {
    const fetchImpl = mockFetch(() =>
      fakeResponse({
        ok: false,
        status: 409,
        json: {
          ok: false,
          error: {
            code: "document_retired",
            message: "This document has been removed from Co-work.",
            retryable: false,
          },
        },
      }),
    );

    await expect(
      transportWith(fetchImpl).push({
        batch: bytes(1),
        baseSha256: "old",
        baseYdocGeneration: "generation-1",
      }),
    ).rejects.toMatchObject({
      apiError: {
        code: "document_retired",
        message: "This document has been removed from Co-work.",
        retryable: false,
        status: 409,
      },
    });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("preserves typed size diagnostics instead of entering stale retry", async () => {
    const fetchImpl = mockFetch(() =>
      fakeResponse({
        ok: false,
        status: 413,
        json: {
          ok: false,
          error: {
            code: "update_too_large",
            message: "opaque update segment exceeds the size limit",
            details: { size_bytes: 12, limit_bytes: 8 },
          },
        },
      }),
    );

    await expect(
      transportWith(fetchImpl).push({
        batch: bytes(1),
        baseSha256: "old",
        baseYdocGeneration: "generation-1",
      }),
    ).rejects.toMatchObject({
      apiError: {
        code: "update_too_large",
        retryable: false,
        status: 413,
        details: { size_bytes: 12, limit_bytes: 8 },
      },
    });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});
