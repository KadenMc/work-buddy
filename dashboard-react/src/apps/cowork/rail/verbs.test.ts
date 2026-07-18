import { describe, expect, it } from "vitest";

import type { ReviewProposal } from "./contracts";
import {
  CLAIM_VERBS,
  CLAIM_VERB_LABEL,
  EDIT_VERBS,
  FLAG_VERBS,
  PROPOSAL_VERB_LABEL,
  isVerbDecidable,
  rejectAsFalseNeedsNegation,
  verbsForProposal,
} from "./verbs";

function proposal(overrides: Partial<ReviewProposal> = {}): ReviewProposal {
  return {
    proposalId: "p1",
    kind: "edit",
    changeType: "insertion",
    quoteAnchor: { exact: "x", prefix: "", suffix: "" },
    replacement: "y",
    rationale: "r",
    tldr: "t",
    producer: { model: "m", modelSource: "s", sessionId: "sid", surface: "mcp" },
    epistemicState: "ai_proposed",
    baseDocSha256: "b",
    canonicalSha256: "c",
    baseOk: true,
    status: "open",
    fixesRef: null,
    claimRefs: [],
    createdAt: "2026-07-17T00:00:00Z",
    anchorLabel: "paragraph 1",
    documentOrder: 1,
    ...overrides,
  };
}

describe("verb label to gesture-kind mapping (section 1.5)", () => {
  it("maps the seven edit UI labels to the shipped kernel kinds", () => {
    const byLabel = Object.fromEntries(
      EDIT_VERBS.map((verb) => [verb.label, verb.verb]),
    );
    expect(byLabel).toEqual({
      Accept: "confirm",
      Amend: "edit_confirm",
      Reject: "reject_plain",
      "Reject as false": "reject_as_false",
      "Reject as preference": "reject_as_preference",
      Redirect: "redirect",
      Defer: "defer",
    });
  });

  it("maps the flag UI labels to endorse, dismiss, and redirect", () => {
    const byLabel = Object.fromEntries(
      FLAG_VERBS.map((verb) => [verb.label, verb.verb]),
    );
    expect(byLabel).toEqual({
      Endorse: "endorse",
      Dismiss: "dismiss",
      Redirect: "redirect",
    });
  });

  it("carries the six committed claim verbs", () => {
    expect(CLAIM_VERBS.map((verb) => verb.verb)).toEqual([
      "confirm",
      "reject",
      "challenge",
      "supersede",
      "redact",
      "propose",
    ]);
    expect(CLAIM_VERBS).toHaveLength(6);
  });

  it("round-trips every proposal and claim verb through its label map", () => {
    for (const verb of [...EDIT_VERBS, ...FLAG_VERBS]) {
      expect(PROPOSAL_VERB_LABEL[verb.verb]).toBe(verb.label);
    }
    for (const verb of CLAIM_VERBS) {
      expect(CLAIM_VERB_LABEL[verb.verb]).toBe(verb.label);
    }
  });

  it("selects the flag verb list for a flag and the edit list otherwise", () => {
    expect(verbsForProposal("flag")).toBe(FLAG_VERBS);
    expect(verbsForProposal("edit")).toBe(EDIT_VERBS);
  });
});

describe("base_ok stale-gate decidability (S6)", () => {
  it("allows every verb on a fresh base", () => {
    const fresh = proposal({ baseOk: true });
    for (const verb of EDIT_VERBS) {
      expect(isVerbDecidable(fresh, verb.verb)).toBe(true);
    }
  });

  it("allows only reject and defer on a stale base", () => {
    const stale = proposal({ baseOk: false });
    expect(isVerbDecidable(stale, "confirm")).toBe(false);
    expect(isVerbDecidable(stale, "edit_confirm")).toBe(false);
    expect(isVerbDecidable(stale, "redirect")).toBe(false);
    expect(isVerbDecidable(stale, "endorse")).toBe(false);
    expect(isVerbDecidable(stale, "reject_plain")).toBe(true);
    expect(isVerbDecidable(stale, "reject_as_false")).toBe(true);
    expect(isVerbDecidable(stale, "reject_as_preference")).toBe(true);
    expect(isVerbDecidable(stale, "defer")).toBe(true);
    expect(isVerbDecidable(stale, "dismiss")).toBe(true);
  });
});

describe("reject_as_false negation requirement (S3)", () => {
  it("needs a verbatim negation when the proposal carries no claim_refs", () => {
    expect(rejectAsFalseNeedsNegation(proposal({ claimRefs: [] }))).toBe(true);
  });

  it("does not need a negation when a claim_ref is present", () => {
    expect(
      rejectAsFalseNeedsNegation(
        proposal({ claimRefs: [{ claim: "c1", role: "instantiation" }] }),
      ),
    ).toBe(false);
  });
});
