import { sha256Hex } from "../../security/localIdentity";
import type { CaptureSmartAvailability } from "./contracts";

/** Canonicalize the displayed boundary synchronously, before any async work. */
export function captureSmartDisclosureSha256(
  disclosure: CaptureSmartAvailability["disclosure"] | undefined,
): Promise<string | undefined> {
  if (disclosure === undefined) return Promise.resolve(undefined);
  // This key order matches the backend's sorted-key disclosure representation.
  return sha256Hex(JSON.stringify({
    maxInputBytes: disclosure.maxInputBytes,
    model: disclosure.model,
    provider: disclosure.provider,
    tools: disclosure.tools,
    web: disclosure.web,
  }));
}
