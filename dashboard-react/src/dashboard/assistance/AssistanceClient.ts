import { normalizeChatExecutionSnapshot } from "../conversations/HttpChatExecutionProfileProvider";
import { exactHumanAuthorityHeaders } from "../../security/humanAuthority";
import type { AssistanceAgent, AssistanceAvailability, AssistancePhase, AssistanceSession, AssistanceStopRequest, AssistanceStopResult } from "./contracts";
import { isRecord } from "./schema";

const PHASES = new Set<AssistancePhase>(["prepared", "starting", "active", "stopped", "ended", "expired", "restart_required"]);

export function assistancePhase(value: unknown): AssistancePhase | undefined {
  return typeof value === "string" && PHASES.has(value as AssistancePhase) ? value as AssistancePhase : undefined;
}

export function assistanceAgent(value: unknown): AssistanceAgent | undefined {
  if (!isRecord(value) || typeof value.status !== "string") return undefined;
  return {
    status: value.status,
    alive: typeof value.alive === "boolean" ? value.alive : null,
    started: value.started === true,
    error: typeof value.error === "string" ? value.error : null,
    phase: assistancePhase(value.phase),
    activeStartId: typeof value.activeStartId === "string" ? value.activeStartId : null,
    controlRevision: Number.isSafeInteger(value.controlRevision) ? value.controlRevision as number : undefined,
  };
}

export class AssistanceRequestError extends Error {
  constructor(message: string, readonly code: string | undefined, readonly payload: unknown, readonly status?: number) {
    super(message);
    this.name = "AssistanceRequestError";
  }
}

export function normalizeAssistanceSession(value: unknown): AssistanceSession {
  if (!isRecord(value) || typeof value.assistantSessionId !== "string" || typeof value.conversationId !== "string" || !isRecord(value.identity) || !isRecord(value.schema)) {
    throw new Error("Assistance returned an invalid draft binding.");
  }
  const protocol = typeof value.protocol === "string" ? value.protocol : undefined;
  const phase = protocol === "wb.assisted-draft.session/v2" ? assistancePhase(value.phase) : "restart_required";
  if (phase === undefined) throw new Error("Assistance returned an unknown session state.");
  return {
    ...value,
    protocol,
    phase,
    activeStartId: typeof value.activeStartId === "string" ? value.activeStartId : null,
    controlRevision: Number.isSafeInteger(value.controlRevision) ? value.controlRevision as number : undefined,
    execution: value.execution === undefined ? undefined : normalizeChatExecutionSnapshot(value.execution),
    agent: assistanceAgent(value.agent),
  } as unknown as AssistanceSession;
}

/** Exact-action transport only. Draft/Chat state and execution CAS stay with their owners. */
export class AssistanceClient {
  readonly fetcher: typeof fetch;

  constructor(fetchImpl?: typeof fetch) {
    this.fetcher = fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  async read(path: string): Promise<unknown> {
    return this.response(await this.fetcher(path, { credentials: "same-origin", headers: { Accept: "application/json" } }));
  }

  authorize(operation: string, subject: string, path: string, body: Record<string, unknown>, method: "POST" | "PATCH" = "POST"): Promise<Record<string, string>> {
    return exactHumanAuthorityHeaders({ action: `dashboard.assistance.${operation}`, subject, context: { method, path, body } }, this.fetcher);
  }

  async post(operation: string, subject: string, path: string, body: Record<string, unknown>, stillCurrent?: () => boolean): Promise<unknown> {
    const headers = await this.authorize(operation, subject, path, body);
    if (stillCurrent && !stillCurrent()) throw new Error("This assistant action was cancelled. Your form is preserved.");
    return this.response(await this.fetcher(path, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json", ...headers },
      body: JSON.stringify(body),
    }));
  }

  async availability(): Promise<AssistanceAvailability> {
    return await this.read("/api/assistance/availability") as AssistanceAvailability;
  }

  async session(sessionId: string): Promise<AssistanceSession> {
    try {
      return normalizeAssistanceSession(await this.read(`/api/assistance/${encodeURIComponent(sessionId)}`));
    } catch (error) {
      if (error instanceof AssistanceRequestError && ["assistance_restart_required", "assistance_session_ended", "assistance_session_expired"].includes(error.code ?? "") && isRecord(error.payload)) {
        const detail = isRecord(error.payload.error) ? error.payload.error : error.payload;
        const retained = detail.session ?? error.payload.session;
        if (isRecord(retained)) {
          if (error.code === "assistance_restart_required") return normalizeAssistanceSession({ ...retained, phase: "restart_required", protocol: undefined });
          const terminalPhase = error.code === "assistance_session_ended" ? "ended" : "expired";
          if (retained.phase === terminalPhase && retained.protocol === "wb.assisted-draft.session/v2") return normalizeAssistanceSession(retained);
        }
      }
      throw error;
    }
  }

  async stop(sessionId: string, request: AssistanceStopRequest): Promise<AssistanceStopResult> {
    let response: unknown;
    try {
      response = await this.cancel(`/api/assistance/${encodeURIComponent(sessionId)}/stop`, request);
    } catch (error) {
      if (error instanceof AssistanceRequestError && error.status === 404 && error.code === "assistance_session_not_found") return { stopped: true, outcome: "already_absent" };
      throw error;
    }
    if (!isRecord(response) || response.stopped !== true || !Number.isSafeInteger(response.controlRevision) || !["stopped", "superseded"].includes(String(response.outcome))) throw new Error("The assistant did not confirm this Stop. Your form remains usable.");
    return response as unknown as AssistanceStopResult;
  }

  async end(sessionId: string): Promise<void> {
    try {
      await this.cancel(`/api/assistance/sessions/${encodeURIComponent(sessionId)}/end`);
    } catch (error) {
      // A typed absence is already the desired terminal state, including
      // after restore/removal. Never absorb arbitrary 404s or auth failures.
      if (!(error instanceof AssistanceRequestError && error.status === 404 && error.code === "assistance_session_not_found")) throw error;
    }
  }

  private async cancel(path: string, body: object = {}): Promise<unknown> {
    // Authority-reducing routes authenticate the same-origin session, not a
    // fresh work gesture. Opt-out/read-only must never prevent cancellation.
    return this.response(await this.fetcher(path, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    }));
  }

  private async response(response: Response): Promise<unknown> {
    const payload: unknown = await response.json();
    if (response.ok) return payload;
    const record = isRecord(payload) ? payload : {};
    const detail = isRecord(record.error) ? record.error : record;
    const message = typeof detail.message === "string" ? detail.message : typeof record.error === "string" ? record.error : "Assistance could not complete this action. Your form is preserved.";
    const code = typeof detail.code === "string" ? detail.code : typeof record.code === "string" ? record.code : undefined;
    throw new AssistanceRequestError(message, code, payload, response.status);
  }
}
