import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  canonicalHumanAuthorityJson,
  coworkHumanAuthorityHeaders,
} from "./humanAuthority";
import { resetLocalIdentityForTests, sha256Hex } from "./localIdentity";

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

describe("exact Co-work human authority", () => {
  it("binds one server session gesture to the exact action, subject, and body", async () => {
    const fetchImpl = vi.fn(
      async (input: RequestInfo | URL, _init?: RequestInit) => {
      if (String(input) === "/api/local-identity/session/csrf") {
        return new Response(
          JSON.stringify({
            ok: true,
            authenticated: true,
            principal,
            csrf_token: "wbc_exact",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          ok: true,
          gesture: {
            token: "wbg_exact",
            action: "cowork.verify.run",
            subject_sha256: "a".repeat(64),
            context_sha256: "b".repeat(64),
            expires_at: 90,
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
      },
    );
    const body = {
      protected_intent: "Preserve the author's meaning.",
      user_goal: "Check the exact target.",
    };

    const headers = await coworkHumanAuthorityHeaders(
      {
        operation: "verify.run",
        storeId: "store-1",
        documentId: "document-1",
        body,
      },
      fetchImpl as typeof fetch,
    );

    expect(headers).toEqual({
      "X-WB-CSRF": "wbc_exact",
      "X-WB-Gesture": "wbg_exact",
    });
    expect(fetchImpl.mock.calls.map(([url]) => String(url))).toEqual([
      "/api/local-identity/session/csrf",
      "/api/local-identity/gestures",
    ]);
    const gestureRequest = fetchImpl.mock.calls[1]?.[1] as RequestInit;
    expect(gestureRequest.headers).toMatchObject({ "X-WB-CSRF": "wbc_exact" });
    const gestureBody = JSON.parse(String(gestureRequest.body));
    expect(gestureBody).toEqual({
      action: "cowork.verify.run",
      subject: "cowork-document:store-1:document-1",
      context_sha256: await sha256Hex(
        canonicalHumanAuthorityJson({
          body,
          document_id: "document-1",
          operation: "verify.run",
          store_id: "store-1",
        }),
      ),
    });
  });
});
