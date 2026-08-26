import { beforeEach, describe, expect, it, vi } from "vitest";

import { exactHumanAuthorityHeaders } from "../../security/humanAuthority";
import { AssistanceClient } from "./AssistanceClient";

vi.mock("../../security/humanAuthority", () => ({ exactHumanAuthorityHeaders: vi.fn(async () => ({ "X-Test-Authority": "exact" })) }));

const response = (body: unknown, status = 200): Response => ({ ok: status < 400, status, json: async () => body }) as Response;

beforeEach(() => { vi.clearAllMocks(); });

describe("AssistanceClient cancellation boundaries", () => {
  it("keeps Stop and End usable without issuing new work authority", async () => {
    const fetcher = vi.fn(async () => response({ ended: true, stopped: true, controlRevision: 2, outcome: "stopped" }));
    const client = new AssistanceClient(fetcher);
    await client.stop("as-one", { requestId: "stop-one", expected_control_revision: 1 });
    await client.end("as-one");
    expect(exactHumanAuthorityHeaders).not.toHaveBeenCalled();
    expect(fetcher.mock.calls).toEqual([
      ["/api/assistance/as-one/stop", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: '{"requestId":"stop-one","expected_control_revision":1}' }],
      ["/api/assistance/sessions/as-one/end", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: "{}" }],
    ]);
  });

  it("acknowledges only typed missing-session End and scoped Stop as already absent", async () => {
    const client = new AssistanceClient(vi.fn(async () => response({ code: "assistance_session_not_found", error: "Session no longer exists" }, 404)));
    await expect(client.end("removed-session")).resolves.toBeUndefined();
    await expect(client.stop("removed-session", { requestId: "stop-one", expected_control_revision: 1 })).resolves.toEqual({ stopped: true, outcome: "already_absent" });
  });

  it.each([
    { body: { error: "Route not found" }, status: 404 },
    { body: { code: "assistance_session_not_found", error: "No access" }, status: 403 },
    { body: { code: "session_expired", error: "Sign in again" }, status: 401 },
  ])("does not mistake $status errors for an End acknowledgement", async ({ body, status }) => {
    const client = new AssistanceClient(vi.fn(async () => response(body, status)));
    await expect(client.end("as-one")).rejects.toMatchObject({ status });
    await expect(client.stop("as-one", { requestId: "stop-one", expected_control_revision: 1 })).rejects.toMatchObject({ status });
  });

  it("rechecks the generation after awaited exact-action authority and before content dispatch", async () => {
    let release: (() => void) | undefined;
    let current = true;
    vi.mocked(exactHumanAuthorityHeaders).mockImplementationOnce(() => new Promise((resolve) => { release = () => resolve({ "X-Test-Authority": "exact" }); }));
    const fetcher = vi.fn(async () => response({}));
    const client = new AssistanceClient(fetcher);
    const request = client.post("start", "assistance:as-one", "/api/assistance/sessions/as-one/start", { initialSnapshot: { snapshot: { title: "Private title" } } }, () => current);
    const rejected = expect(request).rejects.toThrow("cancelled");
    current = false;
    release?.();
    await rejected;
    expect(fetcher).not.toHaveBeenCalled();
  });
});
