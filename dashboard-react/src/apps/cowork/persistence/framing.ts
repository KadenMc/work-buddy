/**
 * Multi-segment blob framing for the R3 / R4 octet-stream bodies (section 1.4). Each
 * segment is prefixed with its length as a 4-byte big-endian unsigned integer, so the
 * client can split an opaque body into its snapshot and update batches without the
 * server ever parsing the Yjs bytes (snapshot first when included, then update batches
 * in append order).
 */

/** Concatenate opaque segments into one length-prefixed body. */
export const frameSegments = (segments: readonly Uint8Array[]): Uint8Array => {
  const total = segments.reduce((sum, segment) => sum + 4 + segment.length, 0);
  const out = new Uint8Array(total);
  const view = new DataView(out.buffer);
  let offset = 0;
  for (const segment of segments) {
    view.setUint32(offset, segment.length, false);
    offset += 4;
    out.set(segment, offset);
    offset += segment.length;
  }
  return out;
};

/** Split a length-prefixed body back into its opaque segments. */
export const parseFrames = (bytes: Uint8Array): Uint8Array[] => {
  const segments: Uint8Array[] = [];
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let offset = 0;
  while (offset < bytes.length) {
    if (offset + 4 > bytes.length) {
      throw new Error("Truncated frame: missing 4-byte length prefix");
    }
    const length = view.getUint32(offset, false);
    offset += 4;
    if (offset + length > bytes.length) {
      throw new Error("Truncated frame: segment shorter than its declared length");
    }
    segments.push(bytes.slice(offset, offset + length));
    offset += length;
  }
  return segments;
};
