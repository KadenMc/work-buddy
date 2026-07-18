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
    expect(pull.batches).toEqual([batchA, batchB]);
    expect(pull.docSha256).toBe("doc");
    expect(pull.nextOffset).toBe("3");
  });

  it("treats an offset-sliced pull as batches only, no snapshot", async () => {
    const batch = bytes(4, 5, 6);
    const fetchImpl = mockFetch(() =>
      fakeResponse({
        headers: { "X-WB-Doc-Sha256": "doc", "X-WB-Next-Offset": "9" },
        body: frameSegments([batch]),
      }),
    );

    const pull = await transportWith(fetchImpl).pull({ sinceOffset: "8" });

    const [, options] = fetchImpl.mock.calls[0];
    expect(options?.headers).toMatchObject({ "X-WB-Since-Offset": "8" });
    expect(pull.snapshot).toBeNull();
    expect(pull.batches).toEqual([batch]);
  });

  it("sends a plain push as the raw batch and reports the applied result", async () => {
    const batch = bytes(1, 1, 2);
    const fetchImpl = mockFetch(() =>
      fakeResponse({
        json: { ok: true, applied: true, doc_sha256: "doc", next_offset: "5" },
      }),
    );

    const result = await transportWith(fetchImpl).push({ batch, baseSha256: "base" });

    const [, options] = fetchImpl.mock.calls[0];
    expect(options?.method).toBe("POST");
    expect(options?.headers).toMatchObject({ "X-WB-Base-Sha256": "base" });
    expect(options?.body).toEqual(batch);
    expect(result).toEqual({
      ok: true,
      applied: true,
      docSha256: "doc",
      nextOffset: "5",
    });
  });

  it("frames a compaction push as batch then snapshot and announces the digest", async () => {
    const batch = bytes(1);
    const snapshot = bytes(2, 2);
    const fetchImpl = mockFetch(() =>
      fakeResponse({
        json: { ok: true, applied: true, doc_sha256: "doc", next_offset: "6" },
      }),
    );

    await transportWith(fetchImpl).push({
      batch,
      baseSha256: "base",
      compaction: { snapshot, snapshotSha256: "snap" },
    });

    const [, options] = fetchImpl.mock.calls[0];
    expect(options?.headers).toMatchObject({
      "X-WB-Compacted-Snapshot-Sha256": "snap",
    });
    const framed = options?.body as Uint8Array;
    expect(parseFrames(framed)).toEqual([batch, snapshot]);
  });

  it("maps a 409 into a stale_base result", async () => {
    const fetchImpl = mockFetch(() =>
      fakeResponse({
        ok: false,
        status: 409,
        json: { ok: false, error: "stale_base", server_doc_sha256: "server" },
      }),
    );

    const result = await transportWith(fetchImpl).push({
      batch: bytes(1),
      baseSha256: "old",
    });

    expect(result).toEqual({
      ok: false,
      error: "stale_base",
      serverDocSha256: "server",
    });
  });
});
