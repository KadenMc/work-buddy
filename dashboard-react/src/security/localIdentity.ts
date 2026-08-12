/** Authenticated loopback identity client for human-authority writes.
 *
 * The server-issued session identifies one enrolled local profile.  A gesture
 * proves only use of the exact bound UI action under the documented local
 * threat model; it is not physical-presence or authorship proof.
 */

export const LOCAL_IDENTITY_AUDIENCE = "work-buddy-dashboard";
export const CSRF_HEADER = "X-WB-CSRF";
export const GESTURE_HEADER = "X-WB-Gesture";

export type ActorRef = {
  schema: "wb.actor-ref/v1";
  issuer_authority_id: string;
  subject: string;
  kind: "human";
  tenant_scope_id: string;
};

export type LocalPrincipal = {
  actor: ActorRef;
  origin: string;
  audience: string;
  session_expires_at: number;
  rotation_due_at: number;
  assurance: "enrolled_local_session";
};

export type LocalIdentityState =
  | { authenticated: false; reason?: string }
  | { authenticated: true; principal: LocalPrincipal };

export type HumanGesture = {
  token: string;
  action: string;
  subject_sha256: string;
  context_sha256: string;
  expires_at: number;
};

type IdentityResponse = {
  ok: boolean;
  authenticated: boolean;
  principal?: LocalPrincipal;
  csrf_token?: string;
  error?: { code?: string; message?: string };
};

type BrowserLocation = Pick<
  Location,
  "hash" | "origin" | "pathname" | "search"
>;

let state: LocalIdentityState = { authenticated: false };
let csrfToken: string | null = null;
let initializePromise: Promise<LocalIdentityState> | null = null;
let refreshPromise: Promise<LocalIdentityState> | null = null;
const listeners = new Set<(next: LocalIdentityState) => void>();

function publish(next: LocalIdentityState): LocalIdentityState {
  state = next;
  for (const listener of listeners) listener(next);
  return next;
}

function responseError(payload: IdentityResponse, fallback: string): Error {
  const error = new Error(payload.error?.message || fallback);
  error.name = payload.error?.code || "LocalIdentityError";
  return error;
}

/**
 * Remove a host-delivered bootstrap from browser history before network I/O.
 * Returns the token only in JS memory and optionally restores the prior hash.
 */
export function consumeBootstrapFragment(
  location: BrowserLocation,
  replaceState: (url: string) => void,
): string | null {
  if (!location.hash.startsWith("#")) return null;
  const fields = new URLSearchParams(location.hash.slice(1));
  const token = fields.get("wb-bootstrap");
  if (!token) return null;

  const next = fields.get("wb-next") || "";
  const safeNext = next.startsWith("#") ? next : "";
  replaceState(`${location.pathname}${location.search}${safeNext}`);
  return token;
}

async function parseIdentityResponse(response: Response): Promise<IdentityResponse> {
  const payload = (await response.json()) as IdentityResponse;
  if (!response.ok || !payload.ok) {
    throw responseError(payload, "The local identity request failed.");
  }
  return payload;
}

async function redeemBootstrap(
  token: string,
  fetchImpl: typeof fetch,
  origin: string,
): Promise<LocalIdentityState> {
  const response = await fetchImpl("/api/local-identity/bootstrap/redeem", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token, audience: LOCAL_IDENTITY_AUDIENCE }),
  });
  const payload = await parseIdentityResponse(response);
  if (!payload.authenticated || !payload.principal || !payload.csrf_token) {
    throw new Error("The local identity bootstrap returned an incomplete session.");
  }
  if (payload.principal.origin !== origin) {
    throw new Error("The local identity session was issued for another Origin.");
  }
  csrfToken = payload.csrf_token;
  return publish({ authenticated: true, principal: payload.principal });
}

async function recoverSession(
  fetchImpl: typeof fetch,
  origin: string,
): Promise<LocalIdentityState> {
  const response = await fetchImpl("/api/local-identity/session/csrf", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  if (response.status === 401 || response.status === 403) {
    publish({ authenticated: false, reason: "No authenticated local session." });
    csrfToken = null;
    return state;
  }
  const payload = await parseIdentityResponse(response);
  if (!payload.authenticated || !payload.principal || !payload.csrf_token) {
    publish({ authenticated: false, reason: "No authenticated local session." });
    csrfToken = null;
    return state;
  }
  if (payload.principal.origin !== origin) {
    throw new Error("The local identity session was issued for another Origin.");
  }
  csrfToken = payload.csrf_token;
  return publish({ authenticated: true, principal: payload.principal });
}

export type InitializeLocalIdentityOptions = {
  fetchImpl?: typeof fetch;
  location?: BrowserLocation;
  replaceState?: (url: string) => void;
};

/** Initialize once at application/provider startup. */
export function initializeLocalIdentity(
  options: InitializeLocalIdentityOptions = {},
): Promise<LocalIdentityState> {
  if (initializePromise) return initializePromise;
  const fetchImpl = options.fetchImpl ?? window.fetch.bind(window);
  const location = options.location ?? window.location;
  const replaceState =
    options.replaceState ??
    ((url: string) => window.history.replaceState(window.history.state, "", url));
  const bootstrap = consumeBootstrapFragment(location, replaceState);
  initializePromise = bootstrap
    ? redeemBootstrap(bootstrap, fetchImpl, location.origin)
    : recoverSession(fetchImpl, location.origin);
  initializePromise = initializePromise.catch((error: unknown) => {
    csrfToken = null;
    publish({
      authenticated: false,
      reason: error instanceof Error ? error.message : "Local identity failed.",
    });
    return state;
  });
  return initializePromise;
}

/**
 * Re-check the browser's local identity boundary.
 *
 * A trusted launcher may focus an already-open dashboard tab by adding a new
 * one-time bootstrap to its URL fragment.  The original initialization has
 * already settled in that case, so retry must deliberately consume the new
 * fragment (or recover a cookie created by another trusted launch) instead of
 * replaying the memoized unauthenticated result.
 */
export function refreshLocalIdentity(
  options: InitializeLocalIdentityOptions = {},
): Promise<LocalIdentityState> {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    if (initializePromise) await initializePromise;
    initializePromise = null;
    return initializeLocalIdentity(options);
  })().finally(() => {
    refreshPromise = null;
  });
  return refreshPromise;
}

export function hasLocalIdentityBootstrap(
  location: Pick<Location, "hash"> = window.location,
): boolean {
  if (!location.hash.startsWith("#")) return false;
  return new URLSearchParams(location.hash.slice(1)).has("wb-bootstrap");
}

export function currentLocalIdentity(): LocalIdentityState {
  return state;
}

export function subscribeLocalIdentity(
  listener: (next: LocalIdentityState) => void,
): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function localIdentityHeaders(gestureToken?: string): Record<string, string> {
  if (!csrfToken || !state.authenticated) {
    throw new Error("An authenticated local session is required.");
  }
  return {
    [CSRF_HEADER]: csrfToken,
    ...(gestureToken ? { [GESTURE_HEADER]: gestureToken } : {}),
  };
}

export async function rotateLocalIdentitySession(
  fetchImpl: typeof fetch = window.fetch.bind(window),
): Promise<LocalIdentityState> {
  const response = await fetchImpl("/api/local-identity/session/rotate", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...localIdentityHeaders(),
    },
    body: "{}",
  });
  const payload = await parseIdentityResponse(response);
  if (!payload.authenticated || !payload.principal || !payload.csrf_token) {
    throw new Error("The rotated local identity session is incomplete.");
  }
  csrfToken = payload.csrf_token;
  return publish({ authenticated: true, principal: payload.principal });
}

async function requestHumanGesture(
  input: { action: string; subject: string; contextSha256: string },
  fetchImpl: typeof fetch,
): Promise<{
  response: Response;
  payload: {
    ok: boolean;
    gesture?: HumanGesture;
    error?: { code?: string; message?: string };
  };
}> {
  const response = await fetchImpl("/api/local-identity/gestures", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...localIdentityHeaders(),
    },
    body: JSON.stringify({
      action: input.action,
      subject: input.subject,
      context_sha256: input.contextSha256,
    }),
  });
  const payload = (await response.json()) as {
    ok: boolean;
    gesture?: HumanGesture;
    error?: { code?: string; message?: string };
  };
  return { response, payload };
}

export async function issueHumanGesture(
  input: { action: string; subject: string; contextSha256: string },
  fetchImpl: typeof fetch = window.fetch.bind(window),
): Promise<HumanGesture> {
  let { response, payload } = await requestHumanGesture(input, fetchImpl);
  if (response.status === 409 && payload.error?.code === "session_rotation_required") {
    await rotateLocalIdentitySession(fetchImpl);
    ({ response, payload } = await requestHumanGesture(input, fetchImpl));
  }
  if (!response.ok || !payload.ok || !payload.gesture) {
    throw responseError(
      { ok: false, authenticated: true, error: payload.error },
      "Could not bind the human-authority action.",
    );
  }
  return payload.gesture;
}

export async function sha256Hex(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

/** Test-only reset for module-scoped browser credentials. */
export function resetLocalIdentityForTests(): void {
  publish({ authenticated: false });
  csrfToken = null;
  initializePromise = null;
  refreshPromise = null;
  listeners.clear();
}
