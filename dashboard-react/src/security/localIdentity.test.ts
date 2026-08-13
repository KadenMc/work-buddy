import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  consumeBootstrapFragment,
  currentLocalIdentity,
  initializeLocalIdentity,
  issueHumanGesture,
  localIdentityHeaders,
  refreshLocalIdentity,
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
  origin: window.location.origin,
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

  it("recovers when a trusted launcher focuses an already-open unauthenticated tab", async () => {
    const location = {
      hash: "",
      origin: principal.origin,
      pathname: "/app/cowork",
      search: "?store_id=store-1&document_id=doc-1",
    };
    const replace = vi.fn((url: string) => {
      location.hash = new URL(url, principal.origin).hash;
    });
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/local-identity/session/csrf") {
        return new Response(
          JSON.stringify({
            ok: true,
            authenticated: false,
            human_authority_available: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      expect(String(input)).toBe("/api/local-identity/bootstrap/redeem");
      return new Response(
        JSON.stringify({
          ok: true,
          authenticated: true,
          principal,
          csrf_token: "wbc_reconnected",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });

    await expect(
      initializeLocalIdentity({ fetchImpl, location, replaceState: replace }),
    ).resolves.toEqual({
      authenticated: false,
      reason: "No authenticated local session.",
    });

    location.hash = "#wb-bootstrap=wbb_reconnect";
    await expect(
      refreshLocalIdentity({ fetchImpl, location, replaceState: replace }),
    ).resolves.toEqual({ authenticated: true, principal });

    expect(fetchImpl.mock.calls.map(([url]) => String(url))).toEqual([
      "/api/local-identity/session/csrf",
      "/api/local-identity/bootstrap/redeem",
    ]);
    expect(replace).toHaveBeenCalledWith(
      "/app/cowork?store_id=store-1&document_id=doc-1",
    );
    expect(localIdentityHeaders()).toEqual({
      "X-WB-CSRF": "wbc_reconnected",
    });
  });

  it("refreshes cached authenticated state and coalesces foreground recovery", async () => {
    let releaseRefresh!: () => void;
    const refreshGate = new Promise<void>((resolve) => {
      releaseRefresh = resolve;
    });
    const fetchImpl = vi.fn(
      async (input: RequestInfo | URL, _init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/local-identity/bootstrap/redeem") {
          return new Response(
            JSON.stringify({
              ok: true,
              authenticated: true,
              principal,
              csrf_token: "wbc_initial",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        expect(url).toBe("/api/local-identity/session/csrf");
        await refreshGate;
        return new Response(
          JSON.stringify({
            ok: true,
            authenticated: true,
            principal: { ...principal, session_expires_at: 199 },
            csrf_token: "wbc_replaced_cookie",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    );
    const location = {
      hash: "#wb-bootstrap=wbb_secret",
      origin: principal.origin,
      pathname: "/app/cowork",
      search: "?document_id=doc-1",
    };
    await initializeLocalIdentity({
      fetchImpl,
      location,
      replaceState: vi.fn(() => {
        location.hash = "";
      }),
    });

    const first = refreshLocalIdentity({ fetchImpl, location });
    const second = refreshLocalIdentity({ fetchImpl, location });
    releaseRefresh();

    await expect(Promise.all([first, second])).resolves.toEqual([
      expect.objectContaining({ authenticated: true }),
      expect.objectContaining({ authenticated: true }),
    ]);
    expect(fetchImpl.mock.calls.map(([url]) => String(url))).toEqual([
      "/api/local-identity/bootstrap/redeem",
      "/api/local-identity/session/csrf",
    ]);
    expect(localIdentityHeaders()).toEqual({
      "X-WB-CSRF": "wbc_replaced_cookie",
    });
  });

  it("refreshes stale CSRF after a trusted cookie replacement and retries once", async () => {
    const gesture = {
      token: "wbg_recovered",
      action: "sources.capture",
      subject_sha256: "a".repeat(64),
      context_sha256: "b".repeat(64),
      expires_at: 90,
    };
    let gestureAttempts = 0;
    const fetchImpl = vi.fn(
      async (input: RequestInfo | URL, _init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/local-identity/bootstrap/redeem") {
          return new Response(
            JSON.stringify({
              ok: true,
              authenticated: true,
              principal,
              csrf_token: "wbc_stale",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url === "/api/local-identity/session/csrf") {
          return new Response(
            JSON.stringify({
              ok: true,
              authenticated: true,
              principal: { ...principal, session_expires_at: 199 },
              csrf_token: "wbc_current",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        expect(url).toBe("/api/local-identity/gestures");
        gestureAttempts += 1;
        if (gestureAttempts === 1) {
          return new Response(
            JSON.stringify({
              ok: false,
              error: { code: "csrf_mismatch", message: "CSRF is stale." },
            }),
            { status: 403, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({ ok: true, gesture }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      },
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

    await expect(
      issueHumanGesture(
        {
          action: "sources.capture",
          subject: "journal:quick-capture",
          contextSha256: "b".repeat(64),
        },
        fetchImpl,
      ),
    ).resolves.toEqual(gesture);

    expect(fetchImpl.mock.calls.map(([url]) => String(url))).toEqual([
      "/api/local-identity/bootstrap/redeem",
      "/api/local-identity/gestures",
      "/api/local-identity/session/csrf",
      "/api/local-identity/gestures",
    ]);
    const retryHeaders = fetchImpl.mock.calls[3]?.[1]?.headers as Record<
      string,
      string
    >;
    expect(retryHeaders["X-WB-CSRF"]).toBe("wbc_current");
  });

  it("publishes unauthenticated state after absolute session expiry", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/local-identity/bootstrap/redeem") {
        return new Response(
          JSON.stringify({
            ok: true,
            authenticated: true,
            principal,
            csrf_token: "wbc_expiring",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === "/api/local-identity/session/csrf") {
        return new Response(
          JSON.stringify({
            ok: true,
            authenticated: false,
            human_authority_available: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      expect(url).toBe("/api/local-identity/gestures");
      return new Response(
        JSON.stringify({
          ok: false,
          error: { code: "session_expired", message: "Session expired." },
        }),
        { status: 401, headers: { "Content-Type": "application/json" } },
      );
    });
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

    await expect(
      issueHumanGesture(
        {
          action: "sources.capture",
          subject: "journal:quick-capture",
          contextSha256: "b".repeat(64),
        },
        fetchImpl,
      ),
    ).rejects.toThrow("Session expired.");

    expect(currentLocalIdentity()).toEqual({
      authenticated: false,
      reason: "Session expired.",
    });
    expect(() => localIdentityHeaders()).toThrow(
      "An authenticated local session is required.",
    );
    expect(
      fetchImpl.mock.calls.filter(
        ([url]) => String(url) === "/api/local-identity/gestures",
      ),
    ).toHaveLength(1);
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
