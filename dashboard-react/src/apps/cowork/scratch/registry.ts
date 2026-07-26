import type { CoworkScratchSummary } from "../contracts";
import { LocalCoworkYdocTransport } from "../persistence/LocalCoworkYdocTransport";

const SCRATCH_REGISTRY_KEY = "work-buddy.cowork.scratches.v1";
export const PREVIOUS_EDITOR_SCRATCH_ID = "cowork-empty";

interface StoredScratchRegistry {
  readonly version: 1;
  readonly scratches: readonly CoworkScratchSummary[];
}

const validScratch = (value: unknown): value is CoworkScratchSummary => {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.scratchId === "string" &&
    typeof item.title === "string" &&
    typeof item.createdAt === "string" &&
    typeof item.updatedAt === "string" &&
    typeof item.recoveredFromPreviousEditor === "boolean"
  );
};

const makeScratchId = (): string => {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid !== undefined) return `scratch-${uuid}`;
  return `scratch-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
};

export class CoworkScratchRegistry {
  readonly #storage: Storage;

  constructor(storage: Storage) {
    this.#storage = storage;
  }

  list(): readonly CoworkScratchSummary[] {
    try {
      const parsed = JSON.parse(this.#storage.getItem(SCRATCH_REGISTRY_KEY) ?? "null") as
        | StoredScratchRegistry
        | null;
      if (parsed?.version !== 1 || !Array.isArray(parsed.scratches)) return [];
      return parsed.scratches.filter(validScratch).sort((left, right) =>
        right.updatedAt.localeCompare(left.updatedAt),
      );
    } catch {
      return [];
    }
  }

  find(scratchId: string): CoworkScratchSummary | undefined {
    return this.list().find((scratch) => scratch.scratchId === scratchId);
  }

  create(title = "Local scratch"): CoworkScratchSummary {
    const now = new Date().toISOString();
    const scratch: CoworkScratchSummary = {
      scratchId: makeScratchId(),
      title: title.trim() || "Local scratch",
      createdAt: now,
      updatedAt: now,
      recoveredFromPreviousEditor: false,
    };
    this.#write([scratch, ...this.list()]);
    return scratch;
  }

  save(scratch: CoworkScratchSummary): void {
    this.#write([
      scratch,
      ...this.list().filter((entry) => entry.scratchId !== scratch.scratchId),
    ]);
  }

  remove(scratchId: string): void {
    this.#write(this.list().filter((scratch) => scratch.scratchId !== scratchId));
  }

  /** Register the previous editor's cowork-empty bytes without moving or deleting them. */
  async discoverPreviousEditorScratch(): Promise<CoworkScratchSummary | null> {
    const existing = this.find(PREVIOUS_EDITOR_SCRATCH_ID);
    if (existing !== undefined) return existing;
    try {
      const persisted = await new LocalCoworkYdocTransport({
        documentId: PREVIOUS_EDITOR_SCRATCH_ID,
      }).pull({});
      if (persisted.snapshot === null && persisted.batches.length === 0) return null;
      const now = new Date().toISOString();
      const recovered: CoworkScratchSummary = {
        scratchId: PREVIOUS_EDITOR_SCRATCH_ID,
        title: "Recovered scratch",
        createdAt: now,
        updatedAt: now,
        recoveredFromPreviousEditor: true,
      };
      this.save(recovered);
      return recovered;
    } catch {
      return null;
    }
  }

  #write(scratches: readonly CoworkScratchSummary[]): void {
    const value: StoredScratchRegistry = { version: 1, scratches };
    this.#storage.setItem(SCRATCH_REGISTRY_KEY, JSON.stringify(value));
  }
}
