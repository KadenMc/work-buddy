/**
 * The same-origin HTTP client for the R2 doc-open read (C1 surface section 1.3), the pull
 * the bridge runs to learn the open proposals, expressions, provenance, and drift for one
 * document. It is a thin fetch wrapper returning the raw R2 payload, so the pure mapper
 * (reviewMapping.ts) owns the translation and this owns only the transport. R2 is a
 * read-only GET, so no consent gate and no read-only rejection apply (those guard the
 * mutating routes). The seam mirrors HttpCoworkYdocTransport and HttpCoworkSittingTransport
 * so a same-origin fetch and an in-memory double are interchangeable.
 */

import type { R2DocPayload } from "./types";

/** The read seam the bridge depends on, satisfied by fetch or an in-memory double. */
export interface CoworkDocClient {
  /** R2 doc-open read for the bound document. */
  fetchDoc(): Promise<R2DocPayload>;
}

export interface HttpCoworkDocClientOptions {
  readonly documentId: string;
  readonly storeId: string;
  /** Injectable for tests, else the global fetch bound to the window. */
  readonly fetchImpl?: typeof fetch;
}

export class HttpCoworkDocClient implements CoworkDocClient {
  readonly #documentId: string;
  readonly #storeId: string;
  readonly #fetch: typeof fetch;

  constructor(options: HttpCoworkDocClientOptions) {
    this.#documentId = options.documentId;
    this.#storeId = options.storeId;
    this.#fetch = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  #endpoint(): string {
    return `/api/truth/doc/${encodeURIComponent(this.#documentId)}?store_id=${encodeURIComponent(this.#storeId)}`;
  }

  async fetchDoc(): Promise<R2DocPayload> {
    const response = await this.#fetch(this.#endpoint(), { method: "GET" });
    if (!response.ok) {
      throw new Error(`doc read failed with status ${String(response.status)}`);
    }
    return (await response.json()) as R2DocPayload;
  }
}
