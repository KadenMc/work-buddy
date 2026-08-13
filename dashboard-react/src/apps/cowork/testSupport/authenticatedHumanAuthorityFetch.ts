import { vi } from "vitest";

type ApplicationResponder = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Response | Promise<Response>;

const jsonResponse = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });

/**
 * Exercise production human-authority acquisition while keeping component and
 * client tests deterministic. Application requests still reach the supplied
 * responder; only the local-session and gesture endpoints are test fixtures.
 */
export const authenticatedHumanAuthorityFetch = (
  respond: ApplicationResponder,
): ReturnType<typeof vi.fn<typeof fetch>> => {
  let gestureIndex = 0;
  return vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input);
    if (url === "/api/local-identity/session/csrf") {
      return jsonResponse({
        ok: true,
        authenticated: true,
        csrf_token: "test-csrf-token",
        principal: {
          actor: {
            schema: "wb.actor-ref/v1",
            issuer_authority_id: "test-issuer",
            subject: "test-dashboard-user",
            kind: "human",
            tenant_scope_id: "test-tenant",
          },
          origin: window.location.origin,
          audience: "work-buddy-dashboard",
          session_expires_at: 9_999_999_999,
          rotation_due_at: 9_999_999_000,
          assurance: "enrolled_local_session",
        },
      });
    }
    if (url === "/api/local-identity/gestures") {
      gestureIndex += 1;
      const request = JSON.parse(String(init?.body)) as {
        action: string;
        context_sha256: string;
      };
      return jsonResponse({
        ok: true,
        gesture: {
          token: `test-gesture-${gestureIndex}`,
          action: request.action,
          subject_sha256: "a".repeat(64),
          context_sha256: request.context_sha256,
          expires_at: 9_999_999_999,
        },
      });
    }
    return respond(input, init);
  });
};

export const applicationRequest = (
  fetchImpl: ReturnType<typeof vi.fn<typeof fetch>>,
): Parameters<typeof fetch> | undefined =>
  fetchImpl.mock.calls.find(
    ([input]) => !String(input).startsWith("/api/local-identity/"),
  );
