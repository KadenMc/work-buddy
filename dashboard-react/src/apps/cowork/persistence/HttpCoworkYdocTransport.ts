/**
 * The same-origin HTTP realization of the Yjs transport seam (R3 pull / R4 push, section
 * 1.4, C3 opaque blobs). It moves opaque bytes only: a pull reads the framed
 * octet-stream body and splits it into the leading snapshot (when the response headers
 * announce one) plus the update batches, and a push sends the raw batch, or the framed
 * batch-then-snapshot pair on a compaction. The server never interprets the Yjs bytes, so
 * this client never asks it to. The in-memory double and this transport are interchangeable
 * behind CoworkYdocTransport.
 */

import { frameSegments, parseFrames } from "./framing";
import type {
  CoworkYdocPull,
  CoworkYdocPullRequest,
  CoworkYdocPushRequest,
  CoworkYdocPushResult,
  CoworkYdocTransport,
} from "./transport";

export interface HttpCoworkYdocTransportOptions {
  readonly documentId: string;
  readonly storeId: string;
  /** Injectable for tests, else the global fetch bound to the window. */
  readonly fetchImpl?: typeof fetch;
}

export class HttpCoworkYdocTransport implements CoworkYdocTransport {
  readonly #documentId: string;
  readonly #storeId: string;
  readonly #fetch: typeof fetch;

  constructor(options: HttpCoworkYdocTransportOptions) {
    this.#documentId = options.documentId;
    this.#storeId = options.storeId;
    this.#fetch = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  #endpoint(): string {
    return `/api/truth/doc/${encodeURIComponent(this.#documentId)}/ydoc?store_id=${encodeURIComponent(this.#storeId)}`;
  }

  async pull(request: CoworkYdocPullRequest): Promise<CoworkYdocPull> {
    const headers: Record<string, string> = {};
    if (request.sinceOffset !== undefined) {
      headers["X-WB-Since-Offset"] = request.sinceOffset;
    }
    const response = await this.#fetch(this.#endpoint(), { method: "GET", headers });
    if (!response.ok) {
      throw new Error(`ydoc pull failed with status ${String(response.status)}`);
    }
    const buffer = new Uint8Array(await response.arrayBuffer());
    const segments = buffer.length > 0 ? parseFrames(buffer) : [];
    const snapshotSha256 = response.headers.get("X-WB-Snapshot-Sha256");
    // A snapshot leads the body only on a full pull (no offset) that announced one.
    const leadsWithSnapshot =
      snapshotSha256 !== null && request.sinceOffset === undefined && segments.length > 0;
    const snapshot = leadsWithSnapshot ? segments[0] : null;
    const batches = leadsWithSnapshot ? segments.slice(1) : segments;
    return {
      snapshot,
      snapshotSha256,
      batches,
      docSha256: response.headers.get("X-WB-Doc-Sha256") ?? "",
      nextOffset: response.headers.get("X-WB-Next-Offset") ?? "",
    };
  }

  async push(request: CoworkYdocPushRequest): Promise<CoworkYdocPushResult> {
    const headers: Record<string, string> = {
      "Content-Type": "application/octet-stream",
      "X-WB-Base-Sha256": request.baseSha256,
    };
    let body: Uint8Array;
    if (request.compaction !== undefined) {
      headers["X-WB-Compacted-Snapshot-Sha256"] = request.compaction.snapshotSha256;
      body = frameSegments([request.batch, request.compaction.snapshot]);
    } else {
      body = request.batch;
    }
    const response = await this.#fetch(this.#endpoint(), {
      method: "POST",
      headers,
      body: body as BodyInit,
    });
    const payload = (await response.json()) as {
      readonly ok?: boolean;
      readonly applied?: boolean;
      readonly doc_sha256?: string;
      readonly next_offset?: string;
      readonly error?: string;
      readonly server_doc_sha256?: string;
    };
    if (response.status === 409 || payload.ok === false) {
      return {
        ok: false,
        error: "stale_base",
        serverDocSha256: payload.server_doc_sha256 ?? "",
      };
    }
    return {
      ok: true,
      applied: Boolean(payload.applied),
      docSha256: payload.doc_sha256 ?? "",
      nextOffset: payload.next_offset ?? "",
    };
  }
}
