import { CoworkHttpError, normalizeCoworkError } from "../providers/errors";
import { coworkHumanAuthorityHeaders } from "../../../security/humanAuthority";
import type {
  CoworkMaterializeReceipt,
  CoworkMaterializeRequest,
} from "./contracts";

type JsonRecord = Record<string, unknown>;

const record = (value: unknown): JsonRecord =>
  typeof value === "object" && value !== null ? (value as JsonRecord) : {};
const text = (value: unknown): string => (typeof value === "string" ? value : "");

export class HttpCoworkMaterializationClient {
  readonly #fetch: typeof fetch;

  constructor(fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis)) {
    this.#fetch = fetchImpl;
  }

  async materialize(
    storeId: string,
    documentId: string,
    request: CoworkMaterializeRequest,
  ): Promise<CoworkMaterializeReceipt> {
    let response: Response;
    try {
      const body = {
        rendered_markdown: request.renderedMarkdown,
        rendered_sha256: request.renderedSha256,
        expected_file_sha256: request.expectedFileSha256,
        expected_ydoc_head_sha256: request.expectedStructuredHeadSha256,
        snapshot_sha256: request.snapshotSha256,
        idempotency_key: request.idempotencyKey,
      };
      const authorityHeaders = await coworkHumanAuthorityHeaders(
        {
          operation: "document.materialize",
          storeId,
          documentId,
          body,
        },
        this.#fetch,
      );
      response = await this.#fetch(
        `/api/truth/doc/${encodeURIComponent(documentId)}/materialize?store_id=${encodeURIComponent(storeId)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", ...authorityHeaders },
          body: JSON.stringify(body),
        },
      );
    } catch (error) {
      throw new CoworkHttpError(normalizeCoworkError(error));
    }
    let payload: unknown = {};
    try {
      payload = await response.json();
    } catch {
      // Status-based normalization below remains actionable.
    }
    if (!response.ok) {
      throw new CoworkHttpError(
        normalizeCoworkError(payload, response.status, "Markdown could not be saved."),
      );
    }
    const data = record(payload);
    const newFileSha256 = text(data.new_file_sha256);
    const structuredHeadSha256 = text(data.structured_head_sha256);
    if (
      !/^[a-f0-9]{64}$/.test(newFileSha256) ||
      !/^[a-f0-9]{64}$/.test(structuredHeadSha256)
    ) {
      throw new CoworkHttpError({
        code: "invalid_materialize_receipt",
        message: "Co-work returned an incomplete Markdown Save receipt.",
        retryable: true,
      });
    }
    return {
      newFileSha256,
      structuredHeadSha256,
      documentVersionId: text(data.document_version_id),
      materializedAt: text(data.materialized_at),
      driftState: "clean",
    };
  }
}
