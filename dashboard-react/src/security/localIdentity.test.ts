import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  consumeBootstrapFragment,
  currentLocalIdentity,
  initializeLocalIdentity,
  issueHumanGesture,
  localIdentityHeaders,
  resetLocalIdentityForTests,
} from "./localIdentity";

const principal = {
  actor: {
    schema: "wb.actor-ref/v1" as const,
    issuer_authority_id: "wia_test",
    subject: "wactor_test",
    kind: "human" as const,
    tenant_scope_id: "wts_test",
  },
  origin: "http://127.0.0.1:5127",
  audience: "work-buddy-dashboard",
  session_expires_at: 99,
  rotation_due_at: 50,
  assurance: "enrolled_local_session" as const,
};

beforeEach(() => resetLocalIdentityForTests());

describe("local identity bootstrap", () => {
  it("scrubs the credential-bearing fragment before returning the token", () => {
    const replace = vi.fn();
    const token = consumeBootstrapFragment(
      {
        hash: "#wb-bootstrap=secret-token&wb-next=%23tab%3Djournal",
        origin: "http://127.0.0.1:5127",
        pathname: "/app/",
        search: "?x=1",
      },
      replace,
    );

    expect(token).toBe("secret-token");
    expect(replace).toHaveBeenCalledWith("/app/?x=1#tab=journal");
    expect(replace.mock.calls[0]?.[0]).not.toContain("secret-token");
  });

  it("redeems once and keeps CSRF out of public identity state", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          ok: true,
          authenticated: true,
          principal,
          csrf_token: "wbc_secret",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    const replace = vi.fn();

    const result = await initializeLocalIdentity({
      fetchImpl,
      location: {
        hash: "#wb-bootstrap=wbb_secret",
        origin: principal.origin,
        pathname: "/app/",
        search: "",
      },
      replaceState: replace,
    });

    expect(result).toEqual({ authenticated: true, principal });
    expect(currentLocalIdentity()).not.toHaveProperty("csrf_token");
    expect(localIdentityHeaders()).toEqual({ "X-WB-CSRF": "wbc_secret" });
    expect(replace).toHaveBeenCalledBefore(fetchImpl);
  });

  it("rotates an aging session once before issuing the bound gesture", async () => {
    const responses = [
      new Response(
        JSON.stringify({
          ok: true,
          authenticated: true,
          principal,
          csrf_token: "wbc_initial",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
      new Response(
        JSON.stringify({
          ok: false,
          error: {
            code: "session_rotation_required",
            message: "Rotate the session.",
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
      new Response(
        JSON.stringify({
          ok: true,
          authenticated: true,
          principal: { ...principal, rotation_due_at: 80 },
          csrf_token: "wbc_rotated",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
      new Response(
        JSON.stringify({
          ok: true,
          gesture: {
            token: "wbg_bound",
            action: "sources.capture",
            subject_sha256: "a".repeat(64),
            context_sha256: "b".repeat(64),
            expires_at: 90,
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    ];
    const fetchImpl = vi.fn().mockImplementation(() =>
      Promise.resolve(responses.shift()),
    );
    await initializeLocalIdentity({
      fetchImpl,
      location: {
        hash: "#wb-bootstrap=wbb_secret",
        origin: principal.origin,
        pathname: "/app/",
        search: "",
      },
      replaceState: vi.fn(),
    });

    const gesture = await issueHumanGesture(
      {
        action: "sources.capture",
        subject: "journal:quick-capture",
        contextSha256: "b".repeat(64),
      },
      fetchImpl,
    );

    expect(gesture.token).toBe("wbg_bound");
    expect(fetchImpl.mock.calls.map(([url]) => url)).toEqual([
      "/api/local-identity/bootstrap/redeem",
      "/api/local-identity/gestures",
      "/api/local-identity/session/rotate",
      "/api/local-identity/gestures",
    ]);
    const finalHeaders = fetchImpl.mock.calls[3]?.[1]?.headers as Record<
      string,
      string
    >;
    expect(finalHeaders["X-WB-CSRF"]).toBe("wbc_rotated");
  });
});
