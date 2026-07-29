import { describe, expect, it } from "vitest";

import { coworkTargetReferenceIdentitySha256 } from "./targetReferenceIdentity";

describe("coworkTargetReferenceIdentitySha256", () => {
  it("matches the server canonical identity and excludes presentation metadata", async () => {
    const base = {
      schema: "wb.cowork.document-target/v1" as const,
      storeId: "store-a",
      documentId: "doc-a",
      kind: "text_range" as const,
      granularity: "character" as const,
      relative: {
        startBase64: "AQID",
        endBase64: "BAUG",
      },
      quote: {
        exact: "  alpha   beta ",
        prefix: "before\nline",
        suffix: " after ",
      },
      label: "Selected passage",
      headingPath: ["Methods"],
      createdAt: "2026-07-29T10:00:00.000Z",
      updatedAt: "2026-07-29T10:00:00.000Z",
    };

    await expect(
      coworkTargetReferenceIdentitySha256(base),
    ).resolves.toBe(
      "46809563029c5d6b664bd3a0610af6da73b0b53afed1c7bf8b3fd5f56683b611",
    );
    await expect(
      coworkTargetReferenceIdentitySha256({
        ...base,
        label: "Working on",
        headingPath: ["Different heading"],
        updatedAt: "2026-07-29T11:00:00.000Z",
      }),
    ).resolves.toBe(
      "46809563029c5d6b664bd3a0610af6da73b0b53afed1c7bf8b3fd5f56683b611",
    );
  });
});
