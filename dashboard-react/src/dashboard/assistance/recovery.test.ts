import { beforeEach, describe, expect, it, vi } from "vitest";

import { AssistanceClient } from "./AssistanceClient";
import { AssistancePauses, AssistanceRevocations } from "./recovery";
import type { AssistanceStopRequest, AssistanceStopResult } from "./contracts";

const clientFor = (end: (id: string) => Promise<void>) => ({ end }) as AssistanceClient;
beforeEach(() => { localStorage.clear(); });

describe("write-ahead assistance cancellation", () => {
  it("writes an irreversible tombstone before End awaits and clears only the acknowledged intent", async () => {
    let acknowledge: (() => void) | undefined;
    const end = vi.fn(async () => {
      expect(localStorage.getItem("wb.assistance.ended:as-one")).toBe("ended");
      expect(JSON.parse(localStorage.getItem("wb.assistance.revocation/v1:as-one")!)).toEqual({ draftKey: "draft-one", sessionId: "as-one" });
      await new Promise<void>((resolve) => { acknowledge = resolve; });
    });
    const queue = new AssistanceRevocations(clientFor(end));
    queue.revoke("draft-one", "as-one");
    expect(queue.isEnded("as-one")).toBe(true);
    acknowledge?.();
    await queue.retry();
    expect(localStorage.getItem("wb.assistance.revocation/v1:as-one")).toBeNull();
    expect(localStorage.getItem("wb.assistance.ended:as-one")).toBe("ended");
    expect(end).toHaveBeenCalledTimes(1);
  });

  it("recovers failed End intent in a fresh controller without resurrecting the binding", async () => {
    const failed = new AssistanceRevocations(clientFor(vi.fn(async () => { throw new Error("Offline"); })));
    failed.revoke("draft-one", "as-one");
    await failed.retry();
    expect(failed.error).toMatch(/not confirmed/);
    const end = vi.fn(async () => undefined);
    const restored = new AssistanceRevocations(clientFor(end));
    expect(restored.isEnded("as-one")).toBe(true);
    await restored.retry();
    expect(end).toHaveBeenCalledWith("as-one");
    expect(restored.error).toBeNull();
    expect(restored.isEnded("as-one")).toBe(true);
  });

  it("does not discard another tab's failed cancellation when its own End succeeds", async () => {
    let acknowledge: (() => void) | undefined;
    const first = new AssistanceRevocations(clientFor(vi.fn(async () => { await new Promise<void>((resolve) => { acknowledge = resolve; }); })));
    const second = new AssistanceRevocations(clientFor(vi.fn(async () => { throw new Error("Offline"); })));
    first.revoke("draft-one", "as-one");
    second.revoke("draft-two", "as-two");
    await second.retry();
    acknowledge?.();
    await first.retry();
    expect(localStorage.getItem("wb.assistance.revocation/v1:as-one")).toBeNull();
    expect(localStorage.getItem("wb.assistance.revocation/v1:as-two")).not.toBeNull();
    expect(second.isEnded("as-two")).toBe(true);
  });
});

describe("generation-scoped assistance Stop recovery", () => {
  it("clears a deleted-session Stop retry after the transport confirms typed absence", async () => {
    const client = new AssistanceClient(vi.fn(async () => ({ ok: false, status: 404, json: async () => ({ code: "assistance_session_not_found", error: "No such session" }) }) as Response));
    const queue = new AssistancePauses(client);
    await expect(queue.pause("draft-one", "removed-session", { requestId: "stop-one", expected_control_revision: 2 })).resolves.toEqual({ stopped: true, outcome: "already_absent" });
    expect(queue.hasPending("removed-session")).toBe(false);
    expect(queue.error).toBeNull();
    expect(localStorage.getItem("wb.assistance.pause/v1:stop-one")).toBeNull();
  });
  it("persists the exact pending-Start fence before making a cleanup request", async () => {
    const request: AssistanceStopRequest = { requestId: "stop-one", expected_control_revision: 0, startRequestId: "pending-start" };
    const stop = vi.fn(async (sessionId: string, body: AssistanceStopRequest): Promise<AssistanceStopResult> => {
      expect(JSON.parse(localStorage.getItem("wb.assistance.pause/v1:stop-one")!)).toEqual({ draftKey: "draft-one", sessionId, request: body });
      return { stopped: true, controlRevision: 2, outcome: "stopped" };
    });
    const queue = new AssistancePauses({ stop } as unknown as AssistanceClient);
    await expect(queue.pause("draft-one", "as-one", request)).resolves.toMatchObject({ outcome: "stopped" });
    expect(stop).toHaveBeenCalledWith("as-one", request);
    expect(queue.hasPending("as-one")).toBe(false);
    expect(localStorage.getItem("wb.assistance.pause/v1:stop-one")).toBeNull();
  });

  it("retries the durable exact Stop after reload and acknowledges a superseded generation without changing it", async () => {
    const request: AssistanceStopRequest = { requestId: "stop-one", expected_control_revision: 1 };
    const first = new AssistancePauses({ stop: vi.fn(async () => { throw new Error("Offline"); }) } as unknown as AssistanceClient);
    await expect(first.pause("draft-one", "as-one", request)).rejects.toThrow("Offline");
    expect(first.hasPending("as-one")).toBe(true);
    expect(first.error).toMatch(/not confirmed/);
    const stop = vi.fn(async (): Promise<AssistanceStopResult> => ({ stopped: true, controlRevision: 4, outcome: "superseded" }));
    const restored = new AssistancePauses({ stop } as unknown as AssistanceClient);
    expect(restored.requestFor("as-one")).toEqual(request);
    await restored.retry();
    expect(stop).toHaveBeenCalledWith("as-one", request);
    expect(restored.hasPending("as-one")).toBe(false);
    expect(restored.error).toBeNull();
    expect(localStorage.getItem("wb.assistance.pause/v1:stop-one")).toBeNull();
  });
});
