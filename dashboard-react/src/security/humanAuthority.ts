/** Exact-body local human-authority headers for protected application actions. */

import {
  initializeLocalIdentity,
  issueHumanGesture,
  localIdentityHeaders,
  sha256Hex,
} from "./localIdentity";

const canonicalize = (value: unknown): string => {
  if (value === null) return "null";
  if (typeof value === "string") return JSON.stringify(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("Gesture context must be finite.");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalize).join(",")}]`;
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, item]) => item !== undefined)
      .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0));
    return `{${entries
      .map(([key, item]) => `${JSON.stringify(key)}:${canonicalize(item)}`)
      .join(",")}}`;
  }
  throw new Error("Gesture context contains an unsupported value.");
};

export async function exactHumanAuthorityHeaders(
  input: {
    readonly action: string;
    readonly subject: string;
    readonly context: unknown;
  },
  fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis),
): Promise<Record<string, string>> {
  const state = await initializeLocalIdentity({ fetchImpl });
  if (!state.authenticated) {
    throw new Error("An authenticated local session is required.");
  }
  const gesture = await issueHumanGesture(
    {
      action: input.action,
      subject: input.subject,
      contextSha256: await sha256Hex(canonicalize(input.context)),
    },
    fetchImpl,
  );
  return localIdentityHeaders(gesture.token);
}

export async function coworkHumanAuthorityHeaders(
  input: {
    readonly operation: string;
    readonly storeId: string;
    readonly documentId: string;
    readonly body: Record<string, unknown>;
  },
  fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis),
): Promise<Record<string, string>> {
  return exactHumanAuthorityHeaders(
    {
      action: `cowork.${input.operation}`,
      subject: `cowork-document:${input.storeId}:${input.documentId}`,
      context: {
        body: input.body,
        document_id: input.documentId,
        operation: input.operation,
        store_id: input.storeId,
      },
    },
    fetchImpl,
  );
}

export { canonicalize as canonicalHumanAuthorityJson };
