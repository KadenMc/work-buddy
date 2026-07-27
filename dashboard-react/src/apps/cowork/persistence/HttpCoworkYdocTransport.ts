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
import {
  CoworkHttpError,
  normalizeCoworkError,
} from "../providers/errors";
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
    const ydocGeneration = response.headers.get("X-WB-Ydoc-Generation")?.trim();
    if (!ydocGeneration) {
      throw new Error("Y.Doc pull response omitted its logical generation");
    }
    const cursorReset = response.headers.get("X-WB-Cursor-Reset") === "1";
    // A stale cursor causes the server to return a complete generation even though the
    // request supplied an offset. The snapshot header, not the request shape, identifies
    // the leading frame. Treating that frame as an ordinary update would merge a replacement
    // document into the stale live Y.Doc instead of replacing it.
    const leadsWithSnapshot = snapshotSha256 !== null && segments.length > 0;
    if (cursorReset && !leadsWithSnapshot) {
      throw new Error(
        "Y.Doc cursor reset response omitted its replacement snapshot",
      );
    }
    const snapshot = leadsWithSnapshot ? segments[0] : null;
    const batches = leadsWithSnapshot ? segments.slice(1) : segments;
    const projectionSha256 =
      response.headers.get("X-WB-Projection-Sha256") ??
      response.headers.get("X-WB-Doc-Sha256") ??
      "";
    const structuredHeadSha256 =
      response.headers.get("X-WB-Ydoc-Head-Sha256") ??
      response.headers.get("X-WB-Structured-Head-Sha256") ??
      response.headers.get("X-WB-Doc-Sha256") ??
      "";
    return {
      snapshot,
      snapshotSha256,
      ydocGeneration,
      batches,
      docSha256: structuredHeadSha256,
      structuredHeadSha256,
      projectionSha256,
      cursorReset,
      nextOffset: response.headers.get("X-WB-Next-Offset") ?? "",
    };
  }

  async push(request: CoworkYdocPushRequest): Promise<CoworkYdocPushResult> {
    const headers: Record<string, string> = {
      "Content-Type": "application/octet-stream",
      "X-WB-Base-Sha256": request.baseSha256,
      "X-WB-Base-Ydoc-Sha256":
        request.baseStructuredHeadSha256 ?? request.baseSha256,
      "X-WB-Base-Ydoc-Generation": request.baseYdocGeneration,
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
    let payload: {
      readonly ok?: boolean;
      readonly applied?: boolean;
      readonly doc_sha256?: string;
      readonly structured_head_sha256?: string;
      readonly ydoc_head_sha256?: string;
      readonly ydoc_generation?: string;
      readonly projection_sha256?: string;
      readonly next_offset?: string;
      readonly error?:
        | string
        | {
            readonly code?: string;
            readonly message?: string;
            readonly retryable?: boolean;
            readonly details?: Readonly<Record<string, unknown>>;
          };
      readonly server_doc_sha256?: string;
      readonly server_structured_head_sha256?: string;
      readonly server_ydoc_generation?: string;
    } = {};
    try {
      payload = (await response.json()) as typeof payload;
    } catch {
      // Status-based normalization below still gives callers a typed failure.
    }
    const errorCode =
      typeof payload.error === "string"
        ? payload.error
        : payload.error?.code;
    if (errorCode === "stale_base") {
      return {
        ok: false,
        error: "stale_base",
        serverDocSha256: payload.server_doc_sha256 ?? "",
        serverStructuredHeadSha256:
          payload.server_structured_head_sha256 ?? payload.server_doc_sha256 ?? "",
        ...(payload.server_ydoc_generation === undefined
          ? {}
          : { serverYdocGeneration: payload.server_ydoc_generation }),
      };
    }
    if (!response.ok || payload.ok === false) {
      throw new CoworkHttpError(
        normalizeCoworkError(
          payload,
          response.status,
          "Co-work could not save the structured document.",
        ),
      );
    }
    const ydocGeneration = payload.ydoc_generation?.trim();
    if (!ydocGeneration) {
      throw new Error("Y.Doc push response omitted its logical generation");
    }
    return {
      ok: true,
      applied: Boolean(payload.applied),
      docSha256:
        payload.structured_head_sha256 ??
        payload.ydoc_head_sha256 ??
        payload.doc_sha256 ??
        "",
      structuredHeadSha256:
        payload.structured_head_sha256 ??
        payload.ydoc_head_sha256 ??
        payload.doc_sha256 ??
        "",
      ydocGeneration,
      projectionSha256: payload.projection_sha256 ?? "",
      nextOffset: payload.next_offset ?? "",
    };
  }
}
