import { describe, expect, it, vi } from "vitest";

import type { CoworkDocClient } from "../../bridge/HttpCoworkDocClient";
import type { R2DocPayload } from "../../bridge/types";
import { LiveProvenanceProvider } from "./LiveProvenanceProvider";

const REVIEWER = {
  ref: "user",
  identityStatus: "local_actor_ref",
} as const;

const WIRE_REVIEWER = {
  kind: "human",
  ref: REVIEWER.ref,
  display_name: null,
  identity_status: REVIEWER.identityStatus,
} as const;

const payload = (history: readonly unknown[] = []): R2DocPayload => ({
  document_id: "doc", store_id: "store", path: "doc.md", title: "Doc", profile: "document",
  hashes: { ydoc_snapshot_sha256: null, last_materialized_sha256: null, current_file_sha256: null },
  drift: { state: "clean", diff_available: false }, open_proposals: [], expressions: [], provenance_spans: [], events_cursor: "",
  provenance: {
    schema: "cowork-provenance-view/v1", current_structured_head_sha256: "a".repeat(64),
    document_default: null, spans: [], history,
    summary: { total_targets: 0, current_span_count: 0, ai_unreviewed_count: 0, reviewed_count: 0, conflicted_count: 0, stale_count: 0, unrecorded: true },
  },
});

describe("LiveProvenanceProvider", () => {
  it("retains one idempotency key and confirms a successor from a fresh pull", async () => {
    const successor = {
      attestation_id: "new", at: "2026-08-12T12:00:00Z",
      asserted_by: { kind: "human", ref: "user", meta: null },
      scope: { kind: "document_span", document_version_id: null, document_span_id: "span", structured_head_sha256: "a".repeat(64) },
      authorship: { kind: "ai", contributors: [] }, human_review: { status: "reviewed", reviewers: [WIRE_REVIEWER] },
      source: { kind: "paste" }, basis: { kind: "user_attestation", ref: "old" }, supersedes_id: "old", canonical_sha256: "b".repeat(64),
    };
    const mutation = vi.fn().mockResolvedValue(undefined);
    const provider = new LiveProvenanceProvider(
      { loadPayload: vi.fn().mockResolvedValue(payload()), refreshPayload: vi.fn().mockResolvedValue(payload([successor])), subscribe: () => () => undefined },
      { fetchDoc: vi.fn(), markProvenanceSelectionReviewed: mutation } as unknown as CoworkDocClient,
    );
    await provider.markReviewed(["old"], "a".repeat(64), REVIEWER);
    expect(mutation).toHaveBeenCalledTimes(1);
    expect(mutation.mock.calls[0]?.[0]).toEqual(["old"]);
    expect(mutation.mock.calls[0]?.[2]).toMatch(/^provenance-review-/u);
    expect(mutation.mock.calls[0]?.[3]).toEqual(REVIEWER);
  });

  it("reuses the same idempotency key when a successful write is not yet visible", async () => {
    const mutation = vi.fn().mockResolvedValue(undefined);
    const refreshPayload = vi.fn().mockResolvedValue(payload());
    const provider = new LiveProvenanceProvider(
      {
        loadPayload: vi.fn().mockResolvedValue(payload()),
        refreshPayload,
        subscribe: () => () => undefined,
      },
      {
        fetchDoc: vi.fn(),
        markProvenanceReviewed: mutation,
      } as unknown as CoworkDocClient,
    );

    await provider.markReviewed(["old"], "a".repeat(64), REVIEWER);
    await provider.markReviewed(["old"], "a".repeat(64), REVIEWER);

    expect(mutation).toHaveBeenCalledTimes(2);
    expect(mutation.mock.calls[1]?.[2]).toBe(mutation.mock.calls[0]?.[2]);
    expect(refreshPayload).toHaveBeenCalledTimes(2);
  });

  it("reuses one batch key until every selected successor is visible", async () => {
    const successor = (prior: string, id: string) => ({
      attestation_id: id,
      at: "2026-08-12T12:00:00Z",
      asserted_by: { kind: "human", ref: "user", meta: null },
      scope: {
        kind: "document_span",
        document_version_id: null,
        document_span_id: `span-${prior}`,
        structured_head_sha256: "a".repeat(64),
      },
      authorship: { kind: "ai", contributors: [] },
      human_review: {
        status: "reviewed",
        reviewers: [WIRE_REVIEWER],
      },
      source: { kind: "paste" },
      basis: { kind: "user_attestation", ref: prior },
      supersedes_id: prior,
      canonical_sha256: "b".repeat(64),
    });
    const first = successor("old-a", "new-a");
    const second = successor("old-b", "new-b");
    const mutation = vi.fn().mockResolvedValue(undefined);
    const refreshPayload = vi
      .fn()
      .mockResolvedValueOnce(payload([first]))
      .mockResolvedValueOnce(payload([first, second]));
    const provider = new LiveProvenanceProvider(
      {
        loadPayload: vi.fn().mockResolvedValue(payload()),
        refreshPayload,
        subscribe: () => () => undefined,
      },
      {
        fetchDoc: vi.fn(),
        markProvenanceSelectionReviewed: mutation,
      } as unknown as CoworkDocClient,
    );

    await provider.markReviewed(
      ["old-a", "old-b"],
      "a".repeat(64),
      REVIEWER,
    );
    await provider.markReviewed(
      ["old-a", "old-b"],
      "a".repeat(64),
      REVIEWER,
    );

    expect(mutation).toHaveBeenCalledTimes(2);
    expect(mutation.mock.calls[1]?.[2]).toBe(mutation.mock.calls[0]?.[2]);
    expect(refreshPayload).toHaveBeenCalledTimes(2);
  });

  it("fails only the provenance surface when rich wire data is malformed", async () => {
    const bad = payload();
    const provider = new LiveProvenanceProvider(
      { loadPayload: vi.fn().mockResolvedValue({ ...bad, provenance: { schema: "bad" } }), refreshPayload: vi.fn(), subscribe: () => () => undefined },
      { fetchDoc: vi.fn() } as unknown as CoworkDocClient,
    );
    await expect(provider.load()).resolves.toMatchObject({ state: "unavailable" });
  });
});
