/**
 * Device-local durability seam shared by the provider (which owns navigation) and the
 * editor (which owns the Y.Doc persistence controller). A session switch is allowed to
 * wait for IndexedDB/local storage, never for the network or Markdown materialization.
 */

export interface CoworkSessionDurabilityLease {
  /** Finish a successful navigation. The old editor must stay paused while it unmounts. */
  commit(): void;
  /** Cancel a failed navigation and make the old editor interactive again. */
  cancel(): void;
}

export interface CoworkSessionDurabilityController {
  prepareToLeave(): Promise<CoworkSessionDurabilityLease>;
}

export interface CoworkSessionDurabilityHooks {
  /** Synchronously stop accepting human input and persistence observation. */
  readonly pause: () => void;
  /** Resume after a navigation was cancelled or failed. */
  readonly resume: () => void;
  /** Retry/await only the device-local write of every already-captured update. */
  readonly ensureDeviceDurability: () => Promise<void>;
}

class HookBackedDurabilityController implements CoworkSessionDurabilityController {
  readonly #hooks: CoworkSessionDurabilityHooks;
  #holds = 0;
  #committed = false;
  #ensureInFlight: Promise<void> | null = null;

  constructor(hooks: CoworkSessionDurabilityHooks) {
    this.#hooks = hooks;
  }

  async prepareToLeave(): Promise<CoworkSessionDurabilityLease> {
    if (this.#holds === 0) this.#hooks.pause();
    this.#holds += 1;
    try {
      if (this.#ensureInFlight === null) {
        const run = this.#hooks.ensureDeviceDurability();
        this.#ensureInFlight = run;
        void run.finally(() => {
          if (this.#ensureInFlight === run) this.#ensureInFlight = null;
        }).catch(() => undefined);
      }
      await this.#ensureInFlight;
    } catch (error) {
      this.#release(false);
      throw error;
    }

    let settled = false;
    return {
      commit: () => {
        if (settled) return;
        settled = true;
        this.#release(true);
      },
      cancel: () => {
        if (settled) return;
        settled = true;
        this.#release(false);
      },
    };
  }

  #release(committed: boolean): void {
    if (committed) this.#committed = true;
    this.#holds = Math.max(0, this.#holds - 1);
    if (this.#holds === 0 && !this.#committed) this.#hooks.resume();
  }
}

class CoworkSessionDurabilityRegistry {
  readonly #controllers = new Map<string, CoworkSessionDurabilityController>();

  register(
    key: string,
    controller: CoworkSessionDurabilityController,
  ): () => void {
    this.#controllers.set(key, controller);
    return () => {
      if (this.#controllers.get(key) === controller) this.#controllers.delete(key);
    };
  }

  prepareToLeave(key: string): Promise<CoworkSessionDurabilityLease> | null {
    return this.#controllers.get(key)?.prepareToLeave() ?? null;
  }
}

export const coworkSessionDurability = new CoworkSessionDurabilityRegistry();

export const createCoworkSessionDurabilityController = (
  hooks: CoworkSessionDurabilityHooks,
): CoworkSessionDurabilityController => new HookBackedDurabilityController(hooks);

export const registeredSessionDurabilityKey = (
  storeId: string,
  documentId: string,
): string => `registered:${storeId}:${documentId}`;

export const scratchSessionDurabilityKey = (scratchId: string): string =>
  `scratch:${scratchId}`;
