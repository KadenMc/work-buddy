import { describe, expect, it } from "vitest";

import { structuredHeadSha256 } from "./structuredHead";

describe("structuredHeadSha256", () => {
  it.each([
    {
      snapshot: new Uint8Array(),
      updates: [],
      digest: "28ca3277b470f732c7f9087e532be8ffca53d81bfc88f107ed5353102f7765ac",
    },
    {
      snapshot: new Uint8Array([0x00, 0x01, 0x02, 0xff]),
      updates: [],
      digest: "5a91acc38df34d983731b7e7e458df87d3f1dc58de4b785f744f9d2ea6fb43df",
    },
    {
      snapshot: new Uint8Array([0x00, 0x01, 0x02, 0xff]),
      updates: [new TextEncoder().encode("alpha"), new Uint8Array([0x00, 0xff, 0x10])],
      digest: "bfe6c9a0388e277d92e2095b77e176ee5327a6735a7cb05ebb6bbb9694bbb8bd",
    },
  ])("matches the Python golden vector $digest", async ({ snapshot, updates, digest }) => {
    await expect(structuredHeadSha256(snapshot, updates)).resolves.toBe(digest);
  });
});
