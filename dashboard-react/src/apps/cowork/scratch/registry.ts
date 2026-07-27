import type { CoworkScratchSummary } from "../contracts";
import { LocalCoworkYdocTransport } from "../persistence/LocalCoworkYdocTransport";
import type { CoworkYdocPull, CoworkYdocPullRequest } from "../persistence/transport";

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

const humanTitle = (scratch: CoworkScratchSummary): CoworkScratchSummary => {
  if (scratch.title === "Local scratch") return { ...scratch, title: "Untitled" };
  if (scratch.title === "Recovered scratch") {
    return { ...scratch, title: "Recovered document" };
  }
  return scratch;
};

const makeScratchId = (): string => {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid !== undefined) return `scratch-${uuid}`;
  return `scratch-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
};

export interface CoworkScratchTransport {
  pull(request: CoworkYdocPullRequest): Promise<CoworkYdocPull>;
  delete(): Promise<void>;
}

export type CoworkScratchTransportFactory = (
  scratchId: string,
) => CoworkScratchTransport;

export class CoworkScratchRegistry {
  readonly #storage: Storage;
  readonly #transportFactory: CoworkScratchTransportFactory;

  constructor(
    storage: Storage,
    transportFactory: CoworkScratchTransportFactory = (scratchId) =>
      new LocalCoworkYdocTransport({ documentId: scratchId }),
  ) {
    this.#storage = storage;
    this.#transportFactory = transportFactory;
  }

  list(): readonly CoworkScratchSummary[] {
    try {
      const parsed = JSON.parse(this.#storage.getItem(SCRATCH_REGISTRY_KEY) ?? "null") as
        | StoredScratchRegistry
        | null;
      if (parsed?.version !== 1 || !Array.isArray(parsed.scratches)) return [];
      return parsed.scratches
        .filter(validScratch)
        .map(humanTitle)
        .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
    } catch {
      return [];
    }
  }

  find(scratchId: string): CoworkScratchSummary | undefined {
    return this.list().find((scratch) => scratch.scratchId === scratchId);
  }

  create(title = "Untitled"): CoworkScratchSummary {
    const now = new Date().toISOString();
    const existing = this.list();
    const requestedTitle = title.trim() || "Untitled";
    let uniqueTitle = requestedTitle;
    if (requestedTitle === "Untitled") {
      const titles = new Set(existing.map((scratch) => scratch.title.toLocaleLowerCase()));
      let suffix = 2;
      while (titles.has(uniqueTitle.toLocaleLowerCase())) {
        uniqueTitle = `Untitled ${suffix}`;
        suffix += 1;
      }
    }
    const scratch: CoworkScratchSummary = {
      scratchId: makeScratchId(),
      title: uniqueTitle,
      createdAt: now,
      updatedAt: now,
      recoveredFromPreviousEditor: false,
    };
    this.#write([scratch, ...existing]);
    return scratch;
  }

  save(scratch: CoworkScratchSummary): void {
    this.#write([
      scratch,
      ...this.list().filter((entry) => entry.scratchId !== scratch.scratchId),
    ]);
  }

  touch(scratchId: string, updatedAt = new Date().toISOString()): CoworkScratchSummary {
    const scratch = this.find(scratchId);
    if (scratch === undefined) {
      throw new Error("This document was not found on this device.");
    }
    const updated = { ...scratch, updatedAt };
    this.save(updated);
    return updated;
  }

  remove(scratchId: string): void {
    this.#write(this.list().filter((scratch) => scratch.scratchId !== scratchId));
  }

  async discard(scratchId: string): Promise<void> {
    await this.#transportFactory(scratchId).delete();
    this.remove(scratchId);
  }

  /** Register the previous editor's cowork-empty bytes without moving or deleting them. */
  async discoverPreviousEditorScratch(): Promise<CoworkScratchSummary | null> {
    const existing = this.find(PREVIOUS_EDITOR_SCRATCH_ID);
    if (existing !== undefined) return existing;
    try {
      const persisted = await this.#transportFactory(PREVIOUS_EDITOR_SCRATCH_ID).pull({});
      if (persisted.snapshot === null && persisted.batches.length === 0) return null;
      const now = new Date().toISOString();
      const recovered: CoworkScratchSummary = {
        scratchId: PREVIOUS_EDITOR_SCRATCH_ID,
        title: "Recovered document",
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
