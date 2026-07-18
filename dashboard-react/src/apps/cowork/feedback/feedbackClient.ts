/**
 * The R9 feedback-capture client (surface section 1.9), the human-initiated
 * route that saves span-anchored feedback VERBATIM as kernel evidence and posts
 * it into the document's conversation. R9 is an HTTP route, never an agent
 * capability (agents author nothing on the user's behalf), so this mirrors the
 * sitting transport: a thin same-origin fetch seam an in-memory double can
 * stand in for.
 *
 * Verified against work_buddy/cowork/api.py::api_doc_feedback and
 * work_buddy/cowork/feedback.py::capture_feedback. Request body:
 *   { span: { exact, prefix, suffix, node_id_hint }, text, conversation_id? }
 * The server reads span.exact (required) and text (required, nonempty), coerces
 * prefix/suffix to "" when absent, and resolves the conversation from the
 * document, so conversation_id is optional and omitted here. Response:
 *   { ok, evidence_id, span_id, conversation_id }.
 */

/** The R9 span selector shape, exactly as the route reads it. */
export interface CoworkFeedbackSpan {
  readonly exact: string;
  readonly prefix: string;
  readonly suffix: string;
  readonly node_id_hint: string | null;
}

export interface CoworkFeedbackRequest {
  readonly documentId: string;
  readonly storeId: string;
  readonly span: CoworkFeedbackSpan;
  /** The feedback text, saved VERBATIM (PRD section 5). */
  readonly text: string;
}

/** The R9 response: the evidence, span, and conversation the feedback landed in. */
export interface CoworkFeedbackResponse {
  readonly ok: boolean;
  readonly evidence_id: string;
  readonly span_id: string;
  readonly conversation_id: string;
}

/** The seam the affordance depends on, satisfied by fetch or an in-memory double. */
export interface CoworkFeedbackTransport {
  submit(request: CoworkFeedbackRequest): Promise<CoworkFeedbackResponse>;
}

/**
 * Same-origin fetch transport for the live route (surface section 1.0, I18). The
 * button click is the consent boundary and the route calls the engine library
 * directly, so this posts JSON to the dashboard service and never touches
 * gestures.
 */
export class HttpCoworkFeedbackTransport implements CoworkFeedbackTransport {
  readonly #fetch: typeof fetch;

  constructor(fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis)) {
    this.#fetch = fetchImpl;
  }

  async submit(request: CoworkFeedbackRequest): Promise<CoworkFeedbackResponse> {
    const url = `/api/truth/doc/${encodeURIComponent(request.documentId)}/feedback?store_id=${encodeURIComponent(request.storeId)}`;
    const response = await this.#fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ span: request.span, text: request.text }),
    });
    if (!response.ok) {
      throw new Error(
        `feedback capture failed with status ${String(response.status)}`,
      );
    }
    return (await response.json()) as CoworkFeedbackResponse;
  }
}

/**
 * In-memory transport double for tests and the offline shell. It records the last
 * request and returns a deterministic capture response derived from the span, so
 * a test asserts both the request the affordance composed and the response it
 * handled.
 */
export class InMemoryCoworkFeedbackTransport implements CoworkFeedbackTransport {
  #lastRequest: CoworkFeedbackRequest | null = null;

  get lastRequest(): CoworkFeedbackRequest | null {
    return this.#lastRequest;
  }

  submit(request: CoworkFeedbackRequest): Promise<CoworkFeedbackResponse> {
    this.#lastRequest = request;
    return Promise.resolve({
      ok: true,
      evidence_id: `ev-${request.documentId}`,
      span_id: `span-${request.documentId}`,
      conversation_id: `cowork-doc-${request.documentId}`,
    });
  }
}
