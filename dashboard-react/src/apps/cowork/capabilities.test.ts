import { describe, expect, it } from "vitest";

import type { CoworkDocumentSummary } from "./contracts";
import {
  coworkRailTabsForCapabilities,
  resolveCoworkDocumentCapabilities,
} from "./capabilities";

const documentWith = (
  truth: "disabled" | "enabled" | "paused" | null,
): Pick<CoworkDocumentSummary, "capabilities"> => ({
  capabilities: {
    schema: "wb.cowork-document-capabilities/v1",
    interactionContract: {
      contractId: "working_document/v1",
      version: 1,
      digest: null,
    },
    modules: { review: true, provenance: true, chat: true, truth: true },
    truth: {
      eligibility: "allowed",
      activation: truth,
      activationRevision: truth === null ? null : 1,
      policyFingerprint: null,
      ledgerPresent: truth === "paused",
      unavailableReason: null,
    },
  },
});

describe("Co-work document capabilities", () => {
  it("keeps the legacy full workspace when the server sends no envelope", () => {
    const resolved = resolveCoworkDocumentCapabilities({});
    expect(resolved.source).toBe("legacy");
    expect(coworkRailTabsForCapabilities(resolved)).toEqual([
      "review",
      "provenance",
      "truth",
      "chat",
    ]);
  });

  it("omits Truth and its projection for a provenance-only document", () => {
    const resolved = resolveCoworkDocumentCapabilities(documentWith("disabled"));
    expect(resolved).toMatchObject({
      source: "server",
      provenance: true,
      truth: false,
      includeTruthProjection: false,
    });
    expect(coworkRailTabsForCapabilities(resolved)).toEqual([
      "review",
      "provenance",
      "chat",
    ]);
  });

  it("keeps a paused Truth ledger visible but read-only", () => {
    expect(resolveCoworkDocumentCapabilities(documentWith("paused"))).toMatchObject({
      truth: true,
      truthReadOnly: true,
      includeTruthProjection: true,
    });
  });
});
