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
 *   { ok, evidence_id, span_id, message_id, conversation_id, agent }.
 */

import {
  normalizeCoworkDocumentAgent,
  type CoworkDocumentAgent,
  type CoworkDocumentAgentStatus,
} from "../chat/documentConversationBinding";
import {
  CoworkHttpError,
  normalizeCoworkError,
} from "../providers/errors";

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
  readonly message_id: string;
  readonly conversation_id: string;
  readonly agent: CoworkDocumentAgent;
}

/** The seam the affordance depends on, satisfied by fetch or an in-memory double. */
export interface CoworkFeedbackTransport {
  submit(request: CoworkFeedbackRequest): Promise<CoworkFeedbackResponse>;
}

const RECONCILIATION_ERROR =
  "Feedback may have been saved, but chat could not confirm it. Reload before trying again.";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null;

const isAgentStatus = (
  value: unknown,
): value is CoworkDocumentAgentStatus =>
  value === "not_started" ||
  value === "running" ||
  value === "stopped" ||
  value === "spawn_failed";

const requiredString = (
  payload: Record<string, unknown>,
  key: string,
): string | null => {
  const value = payload[key];
  return typeof value === "string" && value.trim().length > 0 ? value : null;
};

const normalizeFeedbackResponse = (
  value: unknown,
): CoworkFeedbackResponse => {
  if (!isRecord(value) || value.ok !== true || !isRecord(value.agent)) {
    throw new Error(RECONCILIATION_ERROR);
  }
  const evidenceId = requiredString(value, "evidence_id");
  const spanId = requiredString(value, "span_id");
  const messageId = requiredString(value, "message_id");
  const conversationId = requiredString(value, "conversation_id");
  const rawAgent = value.agent;
  if (
    evidenceId === null ||
    spanId === null ||
    messageId === null ||
    conversationId === null ||
    !isAgentStatus(rawAgent.status) ||
    (rawAgent.alive !== null && typeof rawAgent.alive !== "boolean") ||
    typeof rawAgent.started !== "boolean" ||
    (rawAgent.error !== null && typeof rawAgent.error !== "string")
  ) {
    throw new Error(RECONCILIATION_ERROR);
  }
  return {
    ok: true,
    evidence_id: evidenceId,
    span_id: spanId,
    message_id: messageId,
    conversation_id: conversationId,
    agent: normalizeCoworkDocumentAgent({ agent: rawAgent }),
  };
};

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
    let response: Response;
    try {
      response = await this.#fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ span: request.span, text: request.text }),
      });
    } catch {
      // A disconnected client cannot know whether the authored feedback became
      // durable before the response was lost. Warn against an immediate retry.
      throw new Error(RECONCILIATION_ERROR);
    }

    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      if (response.status >= 400 && response.status < 500) {
        throw new CoworkHttpError(
          normalizeCoworkError(
            undefined,
            response.status,
            "Feedback could not be sent.",
          ),
        );
      }
      throw new Error(RECONCILIATION_ERROR);
    }
    if (!response.ok) {
      if (response.status >= 400 && response.status < 500) {
        throw new CoworkHttpError(
          normalizeCoworkError(
            payload,
            response.status,
            "Feedback could not be sent.",
          ),
        );
      }
      throw new Error(RECONCILIATION_ERROR);
    }
    return normalizeFeedbackResponse(payload);
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
      message_id: `feedback-message-${request.documentId}`,
      conversation_id: `server-feedback-conversation-${request.documentId}`,
      agent: {
        status: "running",
        alive: true,
        started: true,
        error: null,
      },
    });
  }
}
