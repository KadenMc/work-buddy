import { describe, expect, it } from "vitest";

import type { CoworkFolderSummary } from "../contracts";
import {
  coworkStoreUrlId,
  resolveCoworkRouteStoreId,
  resolveCoworkStoreUrlId,
} from "./storeRouteIdentity";

const folder = (storeId: string): Pick<CoworkFolderSummary, "storeId"> => ({ storeId });

describe("Co-work store URL identities", () => {
  it("resolves a unique eight-character prefix to the full catalog ID", () => {
    const fullStoreId = "465142ba386b4f6d84be621efe1425ca";

    expect(resolveCoworkStoreUrlId("465142ba", [folder(fullStoreId)])).toBe(fullStoreId);
    expect(
      resolveCoworkRouteStoreId(
        { kind: "registered", storeId: "465142ba", documentId: "doc-1" },
        [folder(fullStoreId)],
      ),
    ).toEqual({ kind: "registered", storeId: fullStoreId, documentId: "doc-1" });
  });

  it("preserves exact full IDs and exact legacy IDs", () => {
    const fullStoreId = "465142ba386b4f6d84be621efe1425ca";

    expect(resolveCoworkStoreUrlId(fullStoreId, [folder(fullStoreId)])).toBe(fullStoreId);
    expect(resolveCoworkStoreUrlId("store-1", [folder("store-1")])).toBe("store-1");
  });

  it("refuses to resolve an ambiguous prefix", () => {
    const stores = [
      folder("465142ba386b4f6d84be621efe1425ca"),
      folder("465142ba000000000000000000000000"),
    ];

    expect(resolveCoworkStoreUrlId("465142ba", stores)).toBeNull();
    expect(
      resolveCoworkRouteStoreId(
        { kind: "launcher", storeId: "465142ba" },
        stores,
      ),
    ).toEqual({ kind: "launcher", storeId: "465142ba" });
  });

  it("writes a unique prefix but falls back to the full ID on a collision", () => {
    const fullStoreId = "465142ba386b4f6d84be621efe1425ca";

    expect(coworkStoreUrlId(fullStoreId, [folder(fullStoreId)])).toBe("465142ba");
    expect(
      coworkStoreUrlId(fullStoreId, [
        folder(fullStoreId),
        folder("465142ba000000000000000000000000"),
      ]),
    ).toBe(fullStoreId);
  });

  it("does not shorten an ID absent from the current catalog", () => {
    const fullStoreId = "465142ba386b4f6d84be621efe1425ca";

    expect(coworkStoreUrlId(fullStoreId, [])).toBe(fullStoreId);
  });
});
