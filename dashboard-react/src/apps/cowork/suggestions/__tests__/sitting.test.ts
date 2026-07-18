import { describe, expect, it } from "vitest";

import {
  CoworkSittingClient,
  InMemoryCoworkSittingTransport,
  buildMaterializePayload,
  validateSitting,
} from "../sitting";
import type { DecisionItem } from "../types";

describe("validateSitting", () => {
  it("requires amend_content on edit_confirm", () => {
    expect(() =>
      validateSitting(
        [{ proposal_id: "p1", verb: "edit_confirm", canonical_sha256: "c1" }],
        { rendered_markdown: "x", post_apply_content_sha256: "h" },
      ),
    ).toThrow(/amend_content/);
  });

  it("requires redirect_note on redirect", () => {
    expect(() =>
      validateSitting(
        [{ proposal_id: "p1", verb: "redirect", canonical_sha256: "c1" }],
        null,
      ),
    ).toThrow(/redirect_note/);
  });

  it("requires a materialize block when the sitting contains an accept verb", () => {
    expect(() =>
      validateSitting([{ proposal_id: "p1", verb: "confirm", canonical_sha256: "c1" }], null),
    ).toThrow(/materialize/);
  });

  it("forbids a materialize block when the sitting contains no accept verb", () => {
    expect(() =>
      validateSitting([{ proposal_id: "p1", verb: "reject_plain", canonical_sha256: "c1" }], {
        rendered_markdown: "x",
        post_apply_content_sha256: "h",
      }),
    ).toThrow(/materialize/);
  });

  it("accepts a valid mixed sitting", () => {
    expect(() =>
      validateSitting(
        [
          { proposal_id: "p1", verb: "confirm", canonical_sha256: "c1" },
          { proposal_id: "p2", verb: "reject_plain", canonical_sha256: "c2" },
        ],
        { rendered_markdown: "x", post_apply_content_sha256: "h" },
      ),
    ).not.toThrow();
  });
});

describe("buildMaterializePayload", () => {
  it("computes the lowercase hex SHA-256 of the rendered Markdown", async () => {
    const payload = await buildMaterializePayload("hello");
    expect(payload.rendered_markdown).toBe("hello");
    expect(payload.post_apply_content_sha256).toBe(
      "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    );
  });
});

describe("CoworkSittingClient", () => {
  it("composes the frozen R5 body and posts it through the transport", async () => {
    const transport = new InMemoryCoworkSittingTransport();
    const client = new CoworkSittingClient(transport);
    const items: DecisionItem[] = [
      { proposal_id: "p1", verb: "confirm", canonical_sha256: "c1" },
    ];
    const materialize = await buildMaterializePayload("# doc body");

    const response = await client.submit({
      documentId: "doc-42",
      storeId: "store-1",
      baseDocSha256: "base-sha",
      items,
      materialize,
    });

    const request = transport.lastRequest;
    expect(request?.documentId).toBe("doc-42");
    expect(request?.storeId).toBe("store-1");
    expect(request?.body.base_doc_sha256).toBe("base-sha");
    expect(request?.body.items).toEqual(items);
    expect(request?.body.materialize).toEqual(materialize);

    expect(response.ok).toBe(true);
    expect(response.partial).toBe(false);
    expect(response.results[0]).toMatchObject({
      proposal_id: "p1",
      verb: "confirm",
      result: "applied",
      materialized: true,
    });
    expect(response.results[0].gesture_id).not.toBeNull();
    expect(response.materialize?.file_path).toBe("doc-42.md");
  });

  it("maps every verb to its R5 result kind and per-result fields", async () => {
    const transport = new InMemoryCoworkSittingTransport();
    const client = new CoworkSittingClient(transport);
    const items: DecisionItem[] = [
      { proposal_id: "rp", verb: "reject_plain", canonical_sha256: "c" },
      { proposal_id: "rf", verb: "reject_as_false", canonical_sha256: "c", negation_text: "not so" },
      { proposal_id: "rpref", verb: "reject_as_preference", canonical_sha256: "c" },
      { proposal_id: "rd", verb: "redirect", canonical_sha256: "c", redirect_note: "elsewhere" },
      { proposal_id: "df", verb: "defer", canonical_sha256: "c" },
      { proposal_id: "en", verb: "endorse", canonical_sha256: "c" },
      { proposal_id: "ds", verb: "dismiss", canonical_sha256: "c" },
    ];

    const response = await client.submit({
      documentId: "doc-1",
      storeId: "store-1",
      baseDocSha256: "base",
      items,
      materialize: null,
    });

    const byId = new Map(response.results.map((result) => [result.proposal_id, result]));
    expect(byId.get("rp")?.result).toBe("closed");
    expect(byId.get("rf")?.result).toBe("closed");
    expect(byId.get("rf")?.negation_claim_id).toBe("negation-rf");
    expect(byId.get("rpref")?.preference_claim_id).toBe("preference-rpref");
    expect(byId.get("rd")?.result).toBe("kept_open_redirected");
    expect(byId.get("df")?.result).toBe("kept_open_deferred");
    expect(byId.get("en")?.result).toBe("kept_open_endorsed");
    expect(byId.get("ds")?.result).toBe("closed");
    expect(response.materialize).toBeNull();
  });

  it("returns a partial sitting with rejected_stale_view for a stale proposal", async () => {
    const transport = new InMemoryCoworkSittingTransport(["stale"]);
    const client = new CoworkSittingClient(transport);
    const response = await client.submit({
      documentId: "doc-1",
      storeId: "store-1",
      baseDocSha256: "base",
      items: [
        { proposal_id: "stale", verb: "reject_plain", canonical_sha256: "c" },
        { proposal_id: "fresh", verb: "reject_plain", canonical_sha256: "c" },
      ],
      materialize: null,
    });

    expect(response.partial).toBe(true);
    const stale = response.results.find((result) => result.proposal_id === "stale");
    expect(stale?.result).toBe("rejected_stale_view");
    expect(stale?.gesture_id).toBeNull();
    expect(stale?.error).toBe("stale_view");
  });
});
