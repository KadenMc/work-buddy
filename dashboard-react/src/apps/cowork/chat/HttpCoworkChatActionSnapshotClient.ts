import type {
  ChatActionSnapshotContext,
} from "../../../widget-library/chat";
import type { CoworkCapturedActionSnapshot } from "../targets";

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
    const response = await this.#fetch(
      `/api/truth/doc/${encodeURIComponent(this.#documentId)}/chat/action-snapshots?store_id=${encodeURIComponent(this.#storeId)}`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({ capture }),
      },
    );
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      payload = undefined;
    }
    if (!response.ok || !isRecord(payload)) {
      throw new Error(errorMessage(payload, response));
    }
    return normalizeContext(payload.context);
  }
}
