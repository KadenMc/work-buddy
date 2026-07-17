import { describe, expect, it } from "vitest";

import { frameSegments, parseFrames } from "./framing";

describe("frameSegments / parseFrames", () => {
  it("round-trips a single segment", () => {
    const segment = new Uint8Array([1, 2, 3, 4, 5]);
    const framed = frameSegments([segment]);
    // 4-byte length prefix plus the payload.
    expect(framed.length).toBe(4 + 5);
    expect(parseFrames(framed)).toEqual([segment]);
  });

  it("round-trips multiple segments in order", () => {
    const segments = [
      new Uint8Array([9]),
      new Uint8Array([]),
      new Uint8Array([7, 7, 7]),
    ];
    expect(parseFrames(frameSegments(segments))).toEqual(segments);
  });

  it("writes the length as 4-byte big-endian", () => {
    const framed = frameSegments([new Uint8Array(258)]);
    // 258 = 0x00000102 big-endian.
    expect([framed[0], framed[1], framed[2], framed[3]]).toEqual([0, 0, 1, 2]);
  });

  it("throws on a truncated length prefix", () => {
    expect(() => parseFrames(new Uint8Array([0, 0, 1]))).toThrow(/length prefix/);
  });

  it("throws on a segment shorter than its declared length", () => {
    // Declares length 10 but supplies only 2 payload bytes.
    expect(() => parseFrames(new Uint8Array([0, 0, 0, 10, 1, 2]))).toThrow(
      /shorter than its declared length/,
    );
  });
});
