/**
 * Server-owned document-conversation binding.
 *
 * Conversation ids are opaque ids issued by the house conversation store. A
 * document id is never a conversation id and must not be used to manufacture
 * one in the browser. GET discovers an existing binding without side effects;
 * POST /bind is the preparation boundary that may create the binding without
 * starting a model. Sending a message is the separate execution boundary.
 */

import type { FeedbackCapture } from "./contracts";
import { normalizeChatExecutionSnapshot } from "../../../dashboard/conversations";
import type { ChatExecutionSnapshot } from "../../../widget-library/chat";
import { coworkHumanAuthorityHeaders } from "../../../security/humanAuthority";

export type CoworkDocumentAgentStatus =
  | "not_started"
  | "running"
  | "stopped"
  | "spawn_failed";

export interface CoworkDocumentAgent {
  readonly status: CoworkDocumentAgentStatus;
  readonly alive: boolean | null;
  readonly started: boolean;
  readonly error: string | null;
}

export interface CoworkDocumentConversationBinding {
  readonly conversationId: string | null;
  readonly created: boolean;
  readonly agent: CoworkDocumentAgent;
  /** Persisted span annotations for feedback already in this conversation. */
  readonly feedback: readonly FeedbackCapture[];
  /** Server-authoritative provider/model selection and catalog, when supported. */
  readonly execution?: ChatExecutionSnapshot;
}

export interface CoworkDocumentConversationBindingClient {
  load(
    documentId: string,
    storeId: string,
  ): Promise<CoworkDocumentConversationBinding>;
  ensure(
    documentId: string,
    storeId: string,
  ): Promise<CoworkDocumentConversationBinding>;
}

/** A failed binding mutation that still carried newer server authority. */
export class CoworkDocumentConversationBindingError extends Error {
  constructor(
    message: string,
    readonly authoritativeExecution?: ChatExecutionSnapshot,
  ) {
    super(message);
    this.name = "CoworkDocumentConversationBindingError";
  }
}

interface RawBindingPayload {
  readonly ok?: unknown;
  readonly conversation_id?: unknown;
  readonly created?: unknown;
  readonly agent?: unknown;
  readonly agent_status?: unknown;
  readonly agent_error?: unknown;
  readonly feedback?: unknown;
  readonly execution?: unknown;
  readonly error?: unknown;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null;

const errorMessage = (value: unknown): string | null => {
  if (typeof value === "string" && value.trim().length > 0) return value;
  if (!isRecord(value)) return null;
  const message = value.message;
  return typeof message === "string" && message.trim().length > 0
    ? message
    : null;
};

const normalizeStatus = (
  value: unknown,
  alive: boolean | null,
): CoworkDocumentAgentStatus => {
  if (
    value === "not_started" ||
    value === "running" ||
    value === "stopped" ||
    value === "spawn_failed"
  ) {
    return value;
  }
  if (alive === true) return "running";
  if (alive === false) return "stopped";
  return "not_started";
};

/** Parse the canonical nested agent shape, retaining top-level compatibility. */
export const normalizeCoworkDocumentAgent = (
  payload: Pick<
    RawBindingPayload,
    "agent" | "agent_status" | "agent_error"
  >,
): CoworkDocumentAgent => {
  const rawAgent = isRecord(payload.agent) ? payload.agent : {};
  const alive =
    typeof rawAgent.alive === "boolean" ? rawAgent.alive : null;
  const status = normalizeStatus(
    rawAgent.status ?? payload.agent_status,
    alive,
  );
  const rawError = rawAgent.error ?? payload.agent_error;
  return {
    status,
    alive,
    started:
      typeof rawAgent.started === "boolean"
        ? rawAgent.started
        : false,
    error: typeof rawError === "string" && rawError.trim().length > 0
      ? rawError
      : null,
  };
};

const normalizeBinding = (
  payload: RawBindingPayload,
  documentId: string,
  storeId: string,
): CoworkDocumentConversationBinding => {
  const rawId = payload.conversation_id;
  if (rawId !== null && rawId !== undefined && typeof rawId !== "string") {
    throw new Error("The document conversation response was invalid.");
  }
  const conversationId =
    typeof rawId === "string" && rawId.trim().length > 0 ? rawId : null;
  const feedback = normalizeFeedback(
    payload.feedback,
    documentId,
    storeId,
    conversationId,
  );
  return {
    conversationId,
    created: payload.created === true,
    agent: normalizeCoworkDocumentAgent(payload),
    feedback,
    execution:
      payload.execution === undefined
        ? undefined
        : normalizeChatExecutionSnapshot(payload.execution),
  };
};

const requiredString = (
  value: unknown,
  field: string,
): string => {
  if (typeof value === "string" && value.trim().length > 0) return value;
  throw new Error(`The document conversation response had invalid ${field}.`);
};

const normalizeFeedback = (
  value: unknown,
  documentId: string,
  storeId: string,
  boundConversationId: string | null,
): readonly FeedbackCapture[] => {
  if (value === undefined) return [];
  if (!Array.isArray(value)) {
    throw new Error("The document conversation response had invalid feedback.");
  }
  return value.map((raw, index) => {
    if (!isRecord(raw) || !isRecord(raw.anchor)) {
      throw new Error(
        `The document conversation response had invalid feedback[${String(index)}].`,
      );
    }
    const conversationId = requiredString(
      raw.conversation_id,
      `feedback[${String(index)}].conversation_id`,
    );
    if (
      boundConversationId === null ||
      conversationId !== boundConversationId
    ) {
      throw new Error(
        `The document conversation response had mismatched feedback[${String(index)}].conversation_id.`,
      );
    }
    const nodeIdHint = raw.anchor.node_id_hint;
    if (nodeIdHint !== null && typeof nodeIdHint !== "string") {
      throw new Error(
        `The document conversation response had invalid feedback[${String(index)}].anchor.node_id_hint.`,
      );
    }
    return {
      documentId,
      storeId,
      evidenceId: requiredString(
        raw.evidence_id,
        `feedback[${String(index)}].evidence_id`,
      ),
      spanId: requiredString(
        raw.span_id,
        `feedback[${String(index)}].span_id`,
      ),
      conversationId,
      messageId: requiredString(
        raw.message_id,
        `feedback[${String(index)}].message_id`,
      ),
      text: requiredString(raw.text, `feedback[${String(index)}].text`),
      anchor: {
        exact: requiredString(
          raw.anchor.exact,
          `feedback[${String(index)}].anchor.exact`,
        ),
        prefix:
          typeof raw.anchor.prefix === "string" ? raw.anchor.prefix : "",
        suffix:
          typeof raw.anchor.suffix === "string" ? raw.anchor.suffix : "",
        nodeIdHint,
      },
    };
  });
};

const readJson = async (response: Response): Promise<RawBindingPayload> => {
  try {
    const payload: unknown = await response.json();
    return isRecord(payload) ? payload : {};
  } catch {
    return {};
  }
};

export const coworkConversationEndpoint = (
  documentId: string,
  storeId: string,
): string =>
  `/api/truth/doc/${encodeURIComponent(documentId)}/conversation?store_id=${encodeURIComponent(storeId)}`;

export const coworkConversationBindEndpoint = (
  documentId: string,
  storeId: string,
): string =>
  `/api/truth/doc/${encodeURIComponent(documentId)}/conversation/bind?store_id=${encodeURIComponent(storeId)}`;

export const coworkConversationExecutionEndpoint = (
  documentId: string,
  storeId: string,
): string => {
  const query = new URLSearchParams({ store_id: storeId });
  return `/api/truth/doc/${encodeURIComponent(documentId)}/conversation/execution?${query}`;
};

export class HttpCoworkDocumentConversationBindingClient
  implements CoworkDocumentConversationBindingClient
{
  readonly #fetch: typeof fetch;

  constructor(fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis)) {
    this.#fetch = fetchImpl;
  }

  load(
    documentId: string,
    storeId: string,
  ): Promise<CoworkDocumentConversationBinding> {
    return this.#request(documentId, storeId, "GET");
  }

  ensure(
    documentId: string,
    storeId: string,
  ): Promise<CoworkDocumentConversationBinding> {
    return this.#request(documentId, storeId, "POST");
  }

  async #request(
    documentId: string,
    storeId: string,
    method: "GET" | "POST",
  ): Promise<CoworkDocumentConversationBinding> {
    const authorityHeaders =
      method === "POST"
        ? await coworkHumanAuthorityHeaders(
            {
              operation: "chat.bind",
              storeId,
              documentId,
              body: {},
            },
            this.#fetch,
          )
        : {};
    const response = await this.#fetch(
      method === "GET"
        ? coworkConversationEndpoint(documentId, storeId)
        : coworkConversationBindEndpoint(documentId, storeId),
      {
        method,
        headers: { Accept: "application/json", ...authorityHeaders },
      },
    );
    const payload = await readJson(response);
    if (!response.ok || payload.ok !== true) {
      let authoritativeExecution: ChatExecutionSnapshot | undefined;
      if (payload.execution !== undefined) {
        try {
          authoritativeExecution = normalizeChatExecutionSnapshot(
            payload.execution,
          );
        } catch {
          authoritativeExecution = undefined;
        }
      }
      throw new CoworkDocumentConversationBindingError(
        errorMessage(payload.error) ??
          (method === "GET"
            ? "Chat could not be loaded."
            : "Chat could not be prepared. Try again."),
        authoritativeExecution,
      );
    }
    if (!isRecord(payload.agent)) {
      throw new Error("The document conversation response was invalid.");
    }
    return normalizeBinding(payload, documentId, storeId);
  }
}
