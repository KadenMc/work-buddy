import { beforeEach, describe, expect, it } from "vitest";

import type { CoworkDocumentTargetReference } from "./contracts";
import {
  CoworkDocumentTargetStore,
  coworkDocumentTargetStorageKey,
} from "./documentTargetStore";

const reference = (
  overrides: Partial<CoworkDocumentTargetReference> = {},
): CoworkDocumentTargetReference => ({
  schema: "wb.cowork.document-target/v1",
  storeId: "store-a",
  documentId: "doc-a",
  kind: "text_range",
  granularity: "character",
  relative: { startBase64: "AA==", endBase64: "AQ==" },
  quote: { exact: "Risk", prefix: "A ", suffix: " here" },
  label: "Risks",
  headingPath: ["Plan", "Risks"],
  createdAt: "2026-07-28T12:00:00.000Z",
  updatedAt: "2026-07-28T12:00:00.000Z",
  ...overrides,
});

describe("CoworkDocumentTargetStore", () => {
  beforeEach(() => window.localStorage.clear());

  it("round-trips one device-local target per registered document", () => {
    const first = new CoworkDocumentTargetStore({
      storeId: "store-a",
      documentId: "doc-a",
    });
    const second = new CoworkDocumentTargetStore({
      storeId: "store-a",
      documentId: "doc-b",
    });

    first.save(reference());

    expect(first.load()).toEqual(reference());
    expect(second.load()).toBeNull();
  });

  it("drops corrupt or cross-document records instead of applying them", () => {
    const key = coworkDocumentTargetStorageKey("store-a", "doc-a");
    window.localStorage.setItem(key, "{not-json");
    const store = new CoworkDocumentTargetStore({
      storeId: "store-a",
      documentId: "doc-a",
    });
    expect(store.load()).toBeNull();

    window.localStorage.setItem(
      key,
      JSON.stringify(reference({ documentId: "another-doc" })),
    );
    expect(store.load()).toBeNull();
    expect(window.localStorage.getItem(key)).toBeNull();

    window.localStorage.setItem(
      key,
      JSON.stringify({ ...reference(), granularity: "paragraph" }),
    );
    expect(store.load()).toBeNull();
    expect(window.localStorage.getItem(key)).toBeNull();
  });

  it("clears only the current document target", () => {
    const first = new CoworkDocumentTargetStore({
      storeId: "store-a",
      documentId: "doc-a",
    });
    const second = new CoworkDocumentTargetStore({
      storeId: "store-a",
      documentId: "doc-b",
    });
    first.save(reference());
    second.save(reference({ documentId: "doc-b" }));

    first.clear();

    expect(first.load()).toBeNull();
    expect(second.load()?.documentId).toBe("doc-b");
  });

  it("rejects accidental writes for another document", () => {
    const store = new CoworkDocumentTargetStore({
      storeId: "store-a",
      documentId: "doc-a",
    });
    expect(() => store.save(reference({ documentId: "doc-b" }))).toThrow(
      /another Co-work document/u,
    );
  });
});
