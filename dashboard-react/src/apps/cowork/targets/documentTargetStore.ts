import type { CoworkDocumentTargetReference } from "./contracts";

const STORAGE_PREFIX = "wb.cowork.document-target.v1";

const keyFor = (storeId: string, documentId: string): string =>
  `${STORAGE_PREFIX}:${encodeURIComponent(storeId)}:${encodeURIComponent(documentId)}`;

const isStringArray = (value: unknown): value is readonly string[] =>
  Array.isArray(value) && value.every((entry) => typeof entry === "string");

const isReference = (
  value: unknown,
  storeId: string,
  documentId: string,
): value is CoworkDocumentTargetReference => {
  if (value === null || typeof value !== "object") return false;
  const candidate = value as Partial<CoworkDocumentTargetReference>;
  return (
    candidate.schema === "wb.cowork.document-target/v1" &&
    candidate.storeId === storeId &&
    candidate.documentId === documentId &&
    candidate.kind === "text_range" &&
    typeof candidate.relative?.startBase64 === "string" &&
    typeof candidate.relative?.endBase64 === "string" &&
    typeof candidate.quote?.exact === "string" &&
    typeof candidate.quote?.prefix === "string" &&
    typeof candidate.quote?.suffix === "string" &&
    typeof candidate.label === "string" &&
    isStringArray(candidate.headingPath) &&
    typeof candidate.createdAt === "string" &&
    typeof candidate.updatedAt === "string"
  );
};

/**
 * The reusable `Working on` range is presentation state, not collaborative
 * document content. Store it per registered document on this device.
 */
export class CoworkDocumentTargetStore {
  readonly #storeId: string;
  readonly #documentId: string;
  readonly #storage: Storage | null;

  constructor({
    storeId,
    documentId,
    storage,
  }: {
    readonly storeId: string;
    readonly documentId: string;
    readonly storage?: Storage | null;
  }) {
    this.#storeId = storeId;
    this.#documentId = documentId;
    this.#storage =
      storage === undefined
        ? typeof window === "undefined"
          ? null
          : window.localStorage
        : storage;
  }

  load(): CoworkDocumentTargetReference | null {
    if (this.#storage === null) return null;
    try {
      const raw = this.#storage.getItem(keyFor(this.#storeId, this.#documentId));
      if (raw === null) return null;
      const parsed: unknown = JSON.parse(raw);
      if (isReference(parsed, this.#storeId, this.#documentId)) return parsed;
      this.#storage.removeItem(keyFor(this.#storeId, this.#documentId));
      return null;
    } catch {
      try {
        this.#storage.removeItem(keyFor(this.#storeId, this.#documentId));
      } catch {
        // Storage can be unavailable under browser privacy policies. The target
        // remains an in-memory editor concern for this session.
      }
      return null;
    }
  }

  save(reference: CoworkDocumentTargetReference): void {
    if (this.#storage === null) return;
    if (
      reference.storeId !== this.#storeId ||
      reference.documentId !== this.#documentId
    ) {
      throw new Error("Document target belongs to another Co-work document");
    }
    this.#storage.setItem(
      keyFor(this.#storeId, this.#documentId),
      JSON.stringify(reference),
    );
  }

  clear(): void {
    this.#storage?.removeItem(keyFor(this.#storeId, this.#documentId));
  }
}

export const coworkDocumentTargetStorageKey = keyFor;
