import { sha256Hex } from "../persistence/hashing";
import type {
  DecisionItem,
  MaterializePayload,
  SittingItemResult,
  SittingRequest,
  SittingResponse,
} from "./types";

/**
 * The R5 sitting submission client (surface section 1.5), the ONLY decision path. The
 * client collects staged DecisionItems, optionally carries the block-spliced materialize
 * block, and POSTs the frozen R5 shape to /api/truth/doc/<id>/marks. The route mints the
 * gestures, the client mints nothing. The transport is a seam so a same-origin fetch and
 * an in-memory double are interchangeable, and the live route wires at the join.
 */

export interface CoworkSittingRequest {
  readonly documentId: string;
  readonly storeId: string;
  readonly body: SittingRequest;
}

export interface CoworkSittingTransport {
  submit(request: CoworkSittingRequest): Promise<SittingResponse>;
}

/** Verbs that accept a tracked edit and so require the materialize block (surface 1.5). */
const ACCEPT_VERBS = new Set<DecisionItem["verb"]>(["confirm", "edit_confirm"]);

/**
 * Validate a staged sitting against the R5 per-verb rules before it is posted, so a
 * malformed item never reaches the route. amend_content is required for edit_confirm and
 * redirect_note for redirect, and the materialize block is present exactly when the
 * sitting contains any accept verb.
 */
export const validateSitting = (
  items: readonly DecisionItem[],
  materialize: MaterializePayload | null,
): void => {
  for (const item of items) {
    if (item.verb === "edit_confirm" && (item.amend_content ?? "").length === 0) {
      throw new Error(`edit_confirm on ${item.proposal_id} requires amend_content`);
    }
    if (item.verb === "redirect" && (item.redirect_note ?? "").length === 0) {
      throw new Error(`redirect on ${item.proposal_id} requires redirect_note`);
    }
  }
  const hasAccept = items.some((item) => ACCEPT_VERBS.has(item.verb));
  if (hasAccept && materialize === null) {
    throw new Error("a sitting containing an accept verb requires a materialize block");
  }
  if (!hasAccept && materialize !== null) {
    throw new Error("a sitting with no accept verb must not carry a materialize block");
  }
};

/**
 * Build the materialize block from the client-rendered Markdown, computing the content
 * hash so the server can verify rendered_markdown re-hashes to post_apply_content_sha256
 * (surface section 1.5, S5). The client owns the serialization (C3, no server serializer).
 */
export const buildMaterializePayload = async (
  renderedMarkdown: string,
): Promise<MaterializePayload> => ({
  rendered_markdown: renderedMarkdown,
  post_apply_content_sha256: await sha256Hex(new TextEncoder().encode(renderedMarkdown)),
});

export interface SubmitSittingParams {
  readonly documentId: string;
  readonly storeId: string;
  /** Doc hash the whole sitting was composed against (advisory concurrency). */
  readonly baseDocSha256: string;
  readonly items: readonly DecisionItem[];
  readonly materialize: MaterializePayload | null;
}

export class CoworkSittingClient {
  readonly #transport: CoworkSittingTransport;

  constructor(transport: CoworkSittingTransport) {
    this.#transport = transport;
  }

  /** Validate, compose the frozen R5 body, and post it through the transport. */
  async submit(params: SubmitSittingParams): Promise<SittingResponse> {
    validateSitting(params.items, params.materialize);
    const body: SittingRequest = {
      base_doc_sha256: params.baseDocSha256,
      items: [...params.items],
      materialize: params.materialize,
    };
    return this.#transport.submit({
      documentId: params.documentId,
      storeId: params.storeId,
      body,
    });
  }
}

/**
 * Same-origin fetch transport for the live route (surface section 1.0, I18). Wired at the
 * join, not exercised by the unit tests. Routes call the engine library directly and the
 * button click is the consent boundary, so this posts JSON to the dashboard service.
 */
export class HttpCoworkSittingTransport implements CoworkSittingTransport {
  readonly #fetch: typeof fetch;

  constructor(fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis)) {
    this.#fetch = fetchImpl;
  }

  async submit(request: CoworkSittingRequest): Promise<SittingResponse> {
    const url = `/api/truth/doc/${encodeURIComponent(request.documentId)}/marks?store_id=${encodeURIComponent(request.storeId)}`;
    const response = await this.#fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request.body),
    });
    if (!response.ok) {
      throw new Error(`sitting submission failed with status ${String(response.status)}`);
    }
    return (await response.json()) as SittingResponse;
  }
}

/**
 * In-memory sitting transport double for tests and the offline shell. It records the last
 * request and synthesizes a response that maps each verb to its R5 result kind and fields
 * (surface section 1.5), so a test can assert both the request the client composed and the
 * response shape it handled. Ids listed as stale return rejected_stale_view with no
 * gesture, which flips `partial` true.
 */
export class InMemoryCoworkSittingTransport implements CoworkSittingTransport {
  #lastRequest: CoworkSittingRequest | null = null;
  readonly #staleProposalIds: ReadonlySet<string>;

  constructor(staleProposalIds: readonly string[] = []) {
    this.#staleProposalIds = new Set(staleProposalIds);
  }

  get lastRequest(): CoworkSittingRequest | null {
    return this.#lastRequest;
  }

  submit(request: CoworkSittingRequest): Promise<SittingResponse> {
    this.#lastRequest = request;
    const results = request.body.items.map((item) => this.#resultFor(item, request));
    const partial = results.some((result) => result.result !== "applied" && result.gesture_id === null);
    const materialize =
      request.body.materialize === null
        ? null
        : {
            file_path: `${request.documentId}.md`,
            new_file_sha256: request.body.materialize.post_apply_content_sha256,
          };
    return Promise.resolve({ ok: true, partial, results, materialize });
  }

  #resultFor(item: DecisionItem, request: CoworkSittingRequest): SittingItemResult {
    const base: SittingItemResult = {
      proposal_id: item.proposal_id,
      verb: item.verb,
      result: "closed",
      base_ok: true,
      gesture_id: `gesture-${item.proposal_id}`,
      negation_claim_id: null,
      preference_claim_id: null,
      new_proposal_id: null,
      materialized: false,
      error: null,
    };

    if (this.#staleProposalIds.has(item.proposal_id)) {
      return {
        ...base,
        result: "rejected_stale_view",
        gesture_id: null,
        error: "stale_view",
      };
    }

    switch (item.verb) {
      case "confirm":
      case "edit_confirm":
        return {
          ...base,
          result: "applied",
          materialized: request.body.materialize !== null,
        };
      case "reject_plain":
      case "dismiss":
        return base;
      case "reject_as_false":
        return { ...base, negation_claim_id: `negation-${item.proposal_id}` };
      case "reject_as_preference":
        return { ...base, preference_claim_id: `preference-${item.proposal_id}` };
      case "redirect":
        return { ...base, result: "kept_open_redirected" };
      case "defer":
        return { ...base, result: "kept_open_deferred" };
      case "endorse":
        return { ...base, result: "kept_open_endorsed" };
    }
  }
}
