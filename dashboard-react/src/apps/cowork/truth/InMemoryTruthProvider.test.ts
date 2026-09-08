import { describe, expect, it, vi } from "vitest";

import type { TruthSelectionCapture } from "./contracts";
import { InMemoryTruthProvider } from "./InMemoryTruthProvider";

const capture: TruthSelectionCapture = {
  schema: "wb.cowork.truth-selection/v1", captureId: "capture", storeId: "store", documentId: "doc",
  structuredHeadSha256: "head", projectionSha256: "projection", ydocGenerationSha256: "generation",
  label: "Selected passage", wordCount: 3,
  selector: { kind: "text_quote", exact: "An observed result.", prefix: "", suffix: "", start: 10, end: 29 },
};

describe("InMemoryTruthProvider", () => {
  it("preserves the benchmark receipts, current fact, unjudged claims, and multiple passages", async () => {
    const provider = new InMemoryTruthProvider();
    const all = await provider.load({ scope: "document", filter: "all" });
    expect(all.counts).toMatchObject({ all: 4, facts: 1, proposed: 2, needsReview: 1 });
    const fact = all.claims.find((claim) => claim.isFact)!;
    expect((await provider.loadClaim(fact.claimId)).receipts.map((item) => item.sourceLocator)).toEqual([
      "benchmarks/prewarm-a.json", "benchmarks/prewarm-b.json",
    ]);
    expect(all.claims.find((claim) => claim.connectionCount === 2)?.connections).toHaveLength(2);
    expect(provider.getExpressions().every((item) => /\/claim\/[0-9a-f]{32}$/u.test(item.claimRef))).toBe(true);
  });

  it("proposes, connects, confirms and rejects with subscription invalidation and matching editor marks", async () => {
    const provider = new InMemoryTruthProvider({ storeId: "store", documentId: "doc", claims: [] });
    const listener = vi.fn();
    provider.subscribe(listener);
    const proposed = await provider.proposeClaim({ capture, proposition: "A bounded result.", claimKind: "fact", role: "paraphrase" });
    expect(listener).toHaveBeenCalledTimes(1);
    expect((await provider.loadClaim(proposed.claimId!)).receipts).toHaveLength(0);
    expect(provider.getExpressions()).toEqual([expect.objectContaining({ proposition: "A bounded result.", claimStatus: "proposed", isFact: false, evidenceCount: 0 })]);
    const second = { ...capture, selector: { ...capture.selector, exact: "Another expression.", start: 40, end: 59 } };
    await provider.connectClaim({ capture: second, claimId: proposed.claimId!, role: "quote" });
    expect(listener).toHaveBeenCalledTimes(2);
    const bound = await provider.loadClaim(proposed.claimId!);
    await provider.decideClaim({ claimId: bound.claimId, action: "confirm", gestureKind: "confirm", expectedCanonicalSha256: bound.decisionBinding!.payloadSha256, expectedContextSha256: bound.decisionBinding!.contextSha256 });
    expect(listener).toHaveBeenCalledTimes(3);
    expect(provider.getExpressions().every((item) => item.isFact && item.claimStatus === "confirmed")).toBe(true);
    expect((await provider.load({ scope: "document", filter: "facts" })).claims).toHaveLength(1);

    const rejected = await provider.proposeClaim({ capture: second, proposition: "An incorrect result.", claimKind: "fact", role: "quote" });
    const rejectedDetail = await provider.loadClaim(rejected.claimId!);
    await provider.decideClaim({ claimId: rejectedDetail.claimId, action: "reject", expectedCanonicalSha256: rejectedDetail.decisionBinding!.payloadSha256, expectedContextSha256: rejectedDetail.decisionBinding!.contextSha256 });
    expect(listener).toHaveBeenCalledTimes(5);
    expect(provider.getExpressions().find((item) => item.expressionId === rejected.expressionId)).toMatchObject({ claimStatus: "rejected", isFact: false, stale: "claim_terminal" });
  });

  it("rejects a decision whose context changed after it was displayed", async () => {
    const provider = new InMemoryTruthProvider({ storeId: "store", documentId: "doc", claims: [] });
    const proposed = await provider.proposeClaim({ capture, proposition: "A bounded result.", claimKind: "fact", role: "quote" });
    const displayed = await provider.loadClaim(proposed.claimId!);
    await provider.connectClaim({ capture: { ...capture, selector: { ...capture.selector, exact: "Other passage." } }, claimId: displayed.claimId, role: "quote" });
    await expect(provider.decideClaim({ claimId: displayed.claimId, action: "confirm", gestureKind: "confirm", expectedCanonicalSha256: displayed.decisionBinding!.payloadSha256, expectedContextSha256: displayed.decisionBinding!.contextSha256 })).rejects.toThrow("This claim changed");
    expect((await provider.loadClaim(displayed.claimId)).baseStatus).toBe("proposed");
  });

  it("keeps reconnecting the exact passage idempotent and rejects foreign captures", async () => {
    const provider = new InMemoryTruthProvider({ storeId: "store", documentId: "doc", claims: [] });
    const request = { capture, proposition: "A bounded result.", claimKind: "fact", role: "quote" as const };
    const first = await provider.proposeClaim(request);
    const listener = vi.fn();
    provider.subscribe(listener);
    expect(await provider.proposeClaim(request)).toMatchObject({ claimId: first.claimId, claimCreated: false, expressionCreated: false });
    expect(listener).not.toHaveBeenCalled();
    await expect(provider.proposeClaim({ ...request, capture: { ...capture, documentId: "elsewhere" } })).rejects.toThrow("this demo document");
  });
});
