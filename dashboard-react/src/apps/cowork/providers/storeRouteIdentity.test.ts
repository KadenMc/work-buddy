import { describe, expect, it } from "vitest";

import type { CoworkFolderSummary } from "../contracts";
import {
  coworkDocumentUrlId,
  coworkStoreUrlId,
  resolveCoworkDocumentUrlId,
  resolveCoworkRouteStoreId,
  resolveCoworkStoreUrlId,
} from "./storeRouteIdentity";

const folder = (storeId: string): Pick<CoworkFolderSummary, "storeId"> => ({ storeId });
const document = (documentId: string) => ({ documentId });

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

describe("Co-work document URL identities", () => {
  const fullDocumentId = "8f3ff19c111149b88c54c71c7b567d6c";

  it("resolves and writes a unique eight-character prefix inside one Folder", () => {
    expect(resolveCoworkDocumentUrlId("8f3ff19c", [document(fullDocumentId)])).toBe(
      fullDocumentId,
    );
    expect(coworkDocumentUrlId(fullDocumentId, [document(fullDocumentId)])).toBe(
      "8f3ff19c",
    );
  });

  it("preserves exact full and legacy IDs", () => {
    expect(resolveCoworkDocumentUrlId(fullDocumentId, [document(fullDocumentId)])).toBe(
      fullDocumentId,
    );
    expect(resolveCoworkDocumentUrlId("legacy-1", [document("legacy-1")])).toBe(
      "legacy-1",
    );
  });

  it("fails closed for ambiguous or unknown prefixes and writes a full ID on collision", () => {
    const documents = [
      document(fullDocumentId),
      document("8f3ff19c000000000000000000000000"),
    ];

    expect(resolveCoworkDocumentUrlId("8f3ff19c", documents)).toBeNull();
    expect(resolveCoworkDocumentUrlId("00000000", documents)).toBeNull();
    expect(coworkDocumentUrlId(fullDocumentId, documents)).toBe(fullDocumentId);
  });

  it("does not shorten an ID absent from this Folder catalog", () => {
    expect(coworkDocumentUrlId(fullDocumentId, [])).toBe(fullDocumentId);
  });
});
