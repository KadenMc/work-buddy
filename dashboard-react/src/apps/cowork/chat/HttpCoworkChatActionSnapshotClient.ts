import type {
  ChatActionSnapshotContext,
} from "../../../widget-library/chat";
import type { CoworkCapturedActionSnapshot } from "../targets";
import { coworkHumanAuthorityHeaders } from "../../../security/humanAuthority";

type FetchLike = typeof fetch;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null;

const errorMessage = (
  payload: unknown,
  response: Response,
): string => {
  if (
    isRecord(payload) &&
    typeof payload.error === "string" &&
    payload.error.trim().length > 0
  ) {
    return payload.error;
  }
  return response.statusText
    ? `Document context could not be attached. (${response.statusText})`
    : "Document context could not be attached.";
};

const errorCode = (payload: unknown): string | null =>
  isRecord(payload) &&
  typeof payload.code === "string" &&
  payload.code.trim().length > 0
    ? payload.code
    : null;

/** A typed frozen-context failure so the host can recover only safe races. */
export class CoworkChatActionSnapshotError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string | null,
  ) {
    super(message);
    this.name = "CoworkChatActionSnapshotError";
  }
}

const normalizeContext = (value: unknown): ChatActionSnapshotContext => {
  if (
    !isRecord(value) ||
    value.kind !== "action_snapshot" ||
    typeof value.action_snapshot_id !== "string" ||
    typeof value.store_id !== "string" ||
    typeof value.document_id !== "string" ||
    (value.target_kind !== "document" &&
      value.target_kind !== "text_quote") ||
    typeof value.target_label !== "string" ||
    typeof value.target_text_sha256 !== "string" ||
    typeof value.projection_sha256 !== "string" ||
    typeof value.captured_at !== "string"
  ) {
    throw new Error("Co-work returned incomplete frozen document context.");
  }
  return {
    kind: "action_snapshot",
    actionSnapshotId: value.action_snapshot_id,
    storeId: value.store_id,
    documentId: value.document_id,
    targetKind: value.target_kind,
    targetLabel: value.target_label,
    targetWordCount:
      typeof value.target_word_count === "number"
        ? value.target_word_count
        : undefined,
    targetTextSha256: value.target_text_sha256,
    projectionSha256: value.projection_sha256,
    capturedAt: value.captured_at,
  };
};

export interface CoworkChatActionSnapshotClient {
  prepare(
    capture: CoworkCapturedActionSnapshot,
  ): Promise<ChatActionSnapshotContext>;
}

/** Same-origin adapter that freezes context before the Chat message write. */
export class HttpCoworkChatActionSnapshotClient
  implements CoworkChatActionSnapshotClient
{
  readonly #documentId: string;
  readonly #storeId: string;
  readonly #fetch: FetchLike;

  constructor({
    documentId,
    storeId,
    fetchImpl = globalThis.fetch.bind(globalThis),
  }: {
    readonly documentId: string;
    readonly storeId: string;
    readonly fetchImpl?: FetchLike;
  }) {
    this.#documentId = documentId;
    this.#storeId = storeId;
    this.#fetch = fetchImpl;
  }

  async prepare(
    capture: CoworkCapturedActionSnapshot,
  ): Promise<ChatActionSnapshotContext> {
    if (
      capture.documentId !== this.#documentId ||
      capture.storeId !== this.#storeId
    ) {
      throw new Error("Document context belongs to another Co-work document.");
    }
    const body = { capture };
    const authorityHeaders = await coworkHumanAuthorityHeaders(
      {
        operation: "chat.action_snapshot_create",
        storeId: this.#storeId,
        documentId: this.#documentId,
        body,
      },
      this.#fetch,
    );
    const response = await this.#fetch(
      `/api/truth/doc/${encodeURIComponent(this.#documentId)}/chat/action-snapshots?store_id=${encodeURIComponent(this.#storeId)}`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          ...authorityHeaders,
        },
        body: JSON.stringify(body),
      },
    );
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      payload = undefined;
    }
    if (!response.ok || !isRecord(payload)) {
      throw new CoworkChatActionSnapshotError(
        errorMessage(payload, response),
        response.status,
        errorCode(payload),
      );
    }
    return normalizeContext(payload.context);
  }
}
