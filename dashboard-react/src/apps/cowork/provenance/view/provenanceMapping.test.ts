import { describe, expect, it } from "vitest";

import { mapProvenanceView } from "./provenanceMapping";

const record = {
  attestation_id: "attestation-1",
  at: "2026-08-12T12:00:00Z",
  asserted_by: { kind: "human", ref: "user-1", meta: null },
  scope: {
    kind: "document_span",
    document_version_id: null,
    document_span_id: "span-1",
    structured_head_sha256: "a".repeat(64),
  },
  authorship: {
    kind: "ai",
    contributors: [
      {
        kind: "human",
        display_name: "A named collaborator",
        identity_status: "claimed_name",
      },
    ],
  },
  human_review: { status: "not_reviewed", reviewers: [] },
  source: { kind: "paste", format: "plain_text" },
  basis: { kind: "user_attestation", ref: null },
  supersedes_id: null,
  canonical_sha256: "b".repeat(64),
};

const view = (head: string | null = "a".repeat(64)): Record<string, any> => ({
  schema: "cowork-provenance-view/v1",
  current_structured_head_sha256: head,
  document_default: null,
  spans: [
    {
      projection_id: "document_span:span-1",
      target: {
        kind: "document_span",
        document_version_id: null,
        document_span_id: "span-1",
        structured_head_sha256: "a".repeat(64),
        currentness: head === null ? "unavailable" : "current",
      },
      span: { exact: "AI passage", prefix: "", suffix: "" },
      resolution: "resolved",
      review_eligibility: head === null ? "stale_target" : "eligible",
      issue: null,
      effective_attestation: record,
      effective_attestations: [record],
      history: [record],
    },
  ],
  history: [record],
  summary: {
    total_targets: 1,
    current_span_count: head === null ? 0 : 1,
    ai_unreviewed_count: 1,
    reviewed_count: 0,
    conflicted_count: 0,
    stale_count: head === null ? 1 : 0,
    unrecorded: true,
  },
});

describe("mapProvenanceView", () => {
  it("maps rich history while preserving independent authorship and review axes", () => {
    const mapped = mapProvenanceView(view());
    expect(mapped.spans[0]).toMatchObject({
      projectionId: "document_span:span-1",
      reviewEligibility: "eligible",
      effectiveAttestation: {
        humanReview: { status: "not_reviewed" },
        authorship: {
          kind: "ai",
          contributors: [
            {
              label: "A named collaborator",
              identityStatus: "claimed_name",
            },
          ],
        },
      },
    });
  });

  it("keeps history inspectable when the structured head is unavailable", () => {
    const mapped = mapProvenanceView(view(null));
    expect(mapped.currentStructuredHeadSha256).toBeNull();
    expect(mapped.spans[0]?.target.currentness).toBe("unavailable");
  });

  it("fails closed on inconsistent target identity or effective leaves", () => {
    const malformed = structuredClone(view());
    malformed.spans[0].target.document_span_id = null;
    expect(() => mapProvenanceView(malformed)).toThrow("target identity");

    const conflicted = structuredClone(view());
    conflicted.spans[0].resolution = "conflicted";
    expect(() => mapProvenanceView(conflicted)).toThrow("inconsistent resolution");
  });

  it("keeps an orphaned span history inspectable only in its explicit unavailable issue state", () => {
    const orphaned = structuredClone(view());
    orphaned.spans[0].span = null;
    orphaned.spans[0].target.currentness = "unavailable";
    orphaned.spans[0].resolution = "conflicted";
    orphaned.spans[0].review_eligibility = "conflicted";
    orphaned.spans[0].effective_attestation = null;
    orphaned.spans[0].issue = {
      code: "missing_span_target",
      message: "The recorded provenance span is unavailable.",
    };

    expect(mapProvenanceView(orphaned).spans[0]).toMatchObject({
      span: null,
      resolution: "conflicted",
      target: { kind: "document_span", currentness: "unavailable" },
      issue: { code: "missing_span_target" },
    });

    orphaned.spans[0].target.currentness = "current";
    expect(() => mapProvenanceView(orphaned)).toThrow("target identity");
  });

  it("fails closed on an unknown person identity strength", () => {
    const malformed = structuredClone(view());
    malformed.spans[0].effective_attestation.authorship.contributors[0].identity_status =
      "verified_somehow";
    expect(() => mapProvenanceView(malformed)).toThrow("identity_status");
  });
});
