import { sha256Hex } from "./hashing";

const DOMAIN = new TextEncoder().encode("cowork-yjs-structured-head/v1\0");

/** Mirror `truth.ydoc_store.structured_head_from_segments` byte-for-byte. */
export async function structuredHeadSha256(
  snapshot: Uint8Array,
  updates: readonly Uint8Array[],
): Promise<string> {
  const segments = [snapshot, ...updates];
  const size =
    DOMAIN.length + segments.reduce((total, segment) => total + 4 + segment.length, 0);
  const framed = new Uint8Array(size);
  framed.set(DOMAIN, 0);
  const view = new DataView(framed.buffer);
  let offset = DOMAIN.length;
  for (const segment of segments) {
    view.setUint32(offset, segment.length, false);
    offset += 4;
    framed.set(segment, offset);
    offset += segment.length;
  }
  return sha256Hex(framed);
}
