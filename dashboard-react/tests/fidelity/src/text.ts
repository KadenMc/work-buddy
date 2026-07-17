// Byte-level text utilities shared across the fidelity harness.
// Real SHA-256 (lowercase hex, matching the C1 hash convention) and LF
// normalization so the corpus is compared deterministically across platforms.
import { createHash } from "node:crypto";

/** Normalize CRLF to LF. The corpus is stored LF, but a Windows checkout can
 *  reintroduce CRLF, so every read passes through here before comparison. */
export function lf(source: string): string {
  return source.replace(/\r\n/g, "\n");
}

/** Lowercase hex SHA-256 of the UTF-8 bytes of the string. */
export function sha256(source: string): string {
  return createHash("sha256").update(source, "utf8").digest("hex");
}
