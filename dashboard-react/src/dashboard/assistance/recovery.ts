import type { AssistanceClient } from "./AssistanceClient";
import type { AssistanceStopRequest, AssistanceStopResult } from "./contracts";
import { isRecord } from "./schema";

export const assistanceBindingKey = (draftKey: string): string => `wb.assistance.binding:${draftKey}`;
export const assistancePreparationKey = (draftKey: string): string => `wb.assistance.preparation:${draftKey}`;
export const assistanceComposerKey = (draftKey: string, sessionId: string): string => `wb.assistance.composer:${draftKey}:${sessionId}`;
const REVOKE_PREFIX = "wb.assistance.revocation/v1:";
const revokeKey = (sessionId: string): string => `${REVOKE_PREFIX}${sessionId}`;
const tombstoneKey = (sessionId: string): string => `wb.assistance.ended:${sessionId}`;

interface RevokeIntent {
  readonly draftKey: string;
  readonly sessionId: string;
}

export function readSessionValue(key: string): string | null {
  try { return sessionStorage.getItem(key); } catch { return null; }
}

export function writeSessionValue(key: string, value: string): void {
  try { sessionStorage.setItem(key, value); } catch { /* The mounted controller still owns this editing lifetime. */ }
}

export function removeSessionValue(key: string): void {
  try { sessionStorage.removeItem(key); } catch { /* In-memory generation fencing remains mandatory. */ }
}

/** Metadata-only write-ahead cancellation. End acknowledgement never revives a binding. */
export class AssistanceRevocations {
  private readonly intents = new Map<string, RevokeIntent>();
  private readonly ended = new Set<string>();
  private readonly listeners = new Set<() => void>();
  private pending: Promise<void> | null = null;
  private revision = 0;
  private persistenceFailed = false;
  error: string | null = null;

  constructor(private readonly client: AssistanceClient) {
    this.restore();
  }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  readonly getSnapshot = (): number => this.revision;

  isEnded(sessionId: string): boolean {
    if (this.ended.has(sessionId)) return true;
    try { return localStorage.getItem(tombstoneKey(sessionId)) !== null; } catch { return false; }
  }

  /** Must run synchronously before clearing a form, forgetting a binding or awaiting I/O. */
  revoke(draftKey: string, sessionId: string): void {
    this.ended.add(sessionId);
    this.intents.set(sessionId, { draftKey, sessionId });
    try { localStorage.setItem(tombstoneKey(sessionId), "ended"); } catch { this.persistenceFailed = true; }
    this.persist();
    this.changed();
    void this.retry();
  }

  readonly retry = (): Promise<void> => {
    if (this.pending !== null) return this.pending;
    this.restore();
    const intents = [...this.intents.values()];
    if (intents.length === 0) return Promise.resolve();
    this.pending = (async () => {
      let failed = false;
      await Promise.all(intents.map(async (intent) => {
        try {
          await this.client.end(intent.sessionId);
          this.intents.delete(intent.sessionId);
          try { localStorage.removeItem(revokeKey(intent.sessionId)); } catch { this.persistenceFailed = true; }
        } catch { failed = true; }
      }));
      this.persist();
      this.error = failed
        ? `Assistant cancellation is not confirmed yet. Your form remains usable; the old binding will not reopen.${this.persistenceFailed ? " This browser could not retain the cancellation retry after reload." : " Cancellation will retry when you reconnect."}`
        : null;
      this.changed();
    })().finally(() => { this.pending = null; });
    return this.pending;
  };

  private restore(): void {
    try {
      for (let index = 0; index < localStorage.length; index += 1) {
        const key = localStorage.key(index);
        if (!key?.startsWith(REVOKE_PREFIX)) continue;
        let entry: unknown;
        try { entry = JSON.parse(localStorage.getItem(key) ?? "null"); } catch { continue; }
        if (isRecord(entry) && typeof entry.draftKey === "string" && typeof entry.sessionId === "string") {
          this.intents.set(entry.sessionId, { draftKey: entry.draftKey, sessionId: entry.sessionId });
          this.ended.add(entry.sessionId);
        }
      }
    } catch { /* Malformed metadata never reactivates a session. */ }
  }

  private persist(): void {
    // Independent keys prevent one tab's successful End from erasing another
    // tab's still-pending cancellation. Only that session's ack removes it.
    try { for (const intent of this.intents.values()) localStorage.setItem(revokeKey(intent.sessionId), JSON.stringify(intent)); }
    catch { this.persistenceFailed = true; }
  }

  private changed(): void {
    this.revision += 1;
    for (const listener of this.listeners) listener();
  }
}

const PAUSE_PREFIX = "wb.assistance.pause/v1:";
interface PauseIntent {
  readonly draftKey: string;
  readonly sessionId: string;
  readonly request: AssistanceStopRequest;
}
const pauseKey = (requestId: string) => `${PAUSE_PREFIX}${requestId}`;

/** Exact generation-scoped Stop intents. A stale retry is a server-confirmed no-op. */
export class AssistancePauses {
  private readonly intents = new Map<string, PauseIntent>();
  private readonly inFlight = new Map<string, Promise<AssistanceStopResult>>();
  private readonly listeners = new Set<() => void>();
  private revision = 0;
  private persistenceFailed = false;
  error: string | null = null;

  constructor(private readonly client: AssistanceClient) { this.restore(); }
  readonly subscribe = (listener: () => void): (() => void) => { this.listeners.add(listener); return () => this.listeners.delete(listener); };
  readonly getSnapshot = (): number => this.revision;
  hasPending(sessionId: string): boolean { return [...this.intents.values()].some((intent) => intent.sessionId === sessionId); }
  requestFor(sessionId: string): AssistanceStopRequest | undefined { return [...this.intents.values()].find((intent) => intent.sessionId === sessionId)?.request; }

  pause(draftKey: string, sessionId: string, request: AssistanceStopRequest): Promise<AssistanceStopResult> {
    const intent = { draftKey, sessionId, request };
    this.intents.set(request.requestId, intent);
    try { localStorage.setItem(pauseKey(request.requestId), JSON.stringify(intent)); }
    catch { this.persistenceFailed = true; this.error = "Stop is pending, but this browser cannot retain its retry after reload. Keep this page open to retry."; }
    this.changed();
    return this.dispatch(intent);
  }

  readonly retry = async (): Promise<void> => {
    this.restore();
    await Promise.all([...this.intents.values()].map((intent) => this.dispatch(intent).catch(() => undefined)));
  };

  private dispatch(intent: PauseIntent): Promise<AssistanceStopResult> {
    const { requestId } = intent.request;
    const pending = this.inFlight.get(requestId);
    if (pending) return pending;
    const next = this.client.stop(intent.sessionId, intent.request).then((result) => {
      this.intents.delete(requestId);
      try { localStorage.removeItem(pauseKey(requestId)); } catch { /* Exact acknowledged retry is harmless after reload. */ }
      this.error = this.intents.size ? this.error : null;
      this.changed();
      return result;
    }).catch((error: unknown) => {
      this.error = `Assistant Stop is not confirmed yet. The local assistant stays paused, and the exact Stop will retry when you reconnect.${this.persistenceFailed ? " This browser could not retain the retry after reload; keep this page open to retry." : ""}`;
      this.changed();
      throw error;
    }).finally(() => { this.inFlight.delete(requestId); });
    this.inFlight.set(requestId, next);
    return next;
  }

  private restore(): void {
    try {
      for (let index = 0; index < localStorage.length; index += 1) {
        const key = localStorage.key(index);
        if (!key?.startsWith(PAUSE_PREFIX)) continue;
        let value: unknown;
        try { value = JSON.parse(localStorage.getItem(key) ?? "null"); } catch { continue; }
        if (!isRecord(value) || typeof value.draftKey !== "string" || typeof value.sessionId !== "string" || !isRecord(value.request)) continue;
        const request = value.request;
        if (typeof request.requestId !== "string" || !Number.isSafeInteger(request.expected_control_revision) || Number(request.expected_control_revision) < 0 || (request.startRequestId !== undefined && typeof request.startRequestId !== "string")) continue;
        this.intents.set(request.requestId, { draftKey: value.draftKey, sessionId: value.sessionId, request: request as unknown as AssistanceStopRequest });
      }
    } catch { /* An unreadable queue never grants authority to Start. */ }
  }

  private changed(): void { this.revision += 1; for (const listener of this.listeners) listener(); }
}
