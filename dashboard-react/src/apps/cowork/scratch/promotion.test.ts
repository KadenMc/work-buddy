import { describe, expect, it, vi } from "vitest";

import type { CoworkDocumentSummary } from "../contracts";
import { finishScratchPromotion } from "./promotion";

const registeredDocument: CoworkDocumentSummary = {
  documentId: "doc-promoted",
  path: "notes/promoted.md",
  title: "Promoted scratch",
  profile: "co_authored",
  driftState: "clean",
  openProposalCount: 0,
  openFlagCount: 0,
};

describe("finishScratchPromotion", () => {
  it("keeps the scratch recoverable when the registered document cannot open", async () => {
    const openDocument = vi.fn(async () => {
      throw new Error("snapshot hydration failed");
    });
    const retireScratch = vi.fn(async () => undefined);

    await expect(
      finishScratchPromotion(
        registeredDocument,
        "scratch-local",
        openDocument,
        retireScratch,
      ),
    ).rejects.toThrow("snapshot hydration failed");

    expect(retireScratch).not.toHaveBeenCalled();
  });

  it("retires the scratch only after the registered document opens", async () => {
    const order: string[] = [];
    const result = await finishScratchPromotion(
      registeredDocument,
      "scratch-local",
      async () => {
        order.push("opened");
      },
      async () => {
        order.push("retired");
      },
    );

    expect(order).toEqual(["opened", "retired"]);
    expect(result).toEqual({ retired: true });
  });

  it("keeps a cleanup failure recoverable after the registered document opens", async () => {
    const cleanupError = new Error("browser storage stayed busy");
    const result = await finishScratchPromotion(
      registeredDocument,
      "scratch-local",
      async () => undefined,
      async () => {
        throw cleanupError;
      },
    );

    expect(result).toEqual({ retired: false, error: cleanupError });
  });
});
