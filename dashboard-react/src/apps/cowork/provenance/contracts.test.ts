import { describe, expect, it } from "vitest";

import {
  COWORK_PROVENANCE_DETERMINATION_SCHEMA,
  coworkProvenanceDeterminationIssue,
  defaultCoworkProvenanceDetermination,
  type CoworkProvenanceDetermination,
  unknownCoworkProvenanceDetermination,
} from "./contracts";

const ACTOR = {
  kind: "human",
  ref: "dashboard-user",
  identity_status: "local_actor_ref",
} as const;

describe("Co-work provenance determination contract", () => {
  it("starts as current-user human authorship with review not applicable", () => {
    const value = defaultCoworkProvenanceDetermination(ACTOR);

    expect(value).toEqual({
      schema: COWORK_PROVENANCE_DETERMINATION_SCHEMA,
      authorship: {
        kind: "human",
        contributors: [
          {
            kind: "current_user",
            ref: "dashboard-user",
            identity_status: "local_actor_ref",
          },
        ],
      },
      human_review: { status: "not_applicable", reviewers: [] },
    });
    expect(coworkProvenanceDeterminationIssue(value)).toBeNull();
  });

  it("represents a deferred decision as explicit unknown provenance", () => {
    const value = unknownCoworkProvenanceDetermination();

    expect(value).toEqual({
      schema: COWORK_PROVENANCE_DETERMINATION_SCHEMA,
      authorship: { kind: "unknown", contributors: [] },
      human_review: { status: "not_applicable", reviewers: [] },
    });
    expect(coworkProvenanceDeterminationIssue(value)).toBeNull();
  });

  it("keeps authorship and human review as independent attributed facts", () => {
    const value: CoworkProvenanceDetermination = {
      schema: COWORK_PROVENANCE_DETERMINATION_SCHEMA,
      authorship: {
        kind: "mixed",
        contributors: [
          {
            kind: "named_person",
            display_name: "Avery",
          },
        ],
      },
      human_review: {
        status: "reviewed",
        reviewers: [
          {
            kind: "named_person",
            display_name: "Morgan",
          },
        ],
      },
    };

    expect(coworkProvenanceDeterminationIssue(value)).toBeNull();
  });

  it("refuses empty named identities and incompatible review states", () => {
    const unnamed: CoworkProvenanceDetermination = {
      schema: COWORK_PROVENANCE_DETERMINATION_SCHEMA,
      authorship: {
        kind: "human",
        contributors: [{ kind: "named_person", display_name: " " }],
      },
      human_review: { status: "not_applicable", reviewers: [] },
    };
    expect(coworkProvenanceDeterminationIssue(unnamed)).toBe(
      "Enter the author’s name.",
    );

    const unreviewedHuman: CoworkProvenanceDetermination = {
      ...defaultCoworkProvenanceDetermination(ACTOR),
      human_review: { status: "not_reviewed", reviewers: [] },
    };
    expect(coworkProvenanceDeterminationIssue(unreviewedHuman)).toBe(
      "Human review does not apply to text recorded as human-written.",
    );
  });

  it("refuses a legacy current-user placeholder without a captured actor ref", () => {
    const unbound = {
      ...defaultCoworkProvenanceDetermination(ACTOR),
      authorship: {
        kind: "human",
        contributors: [{ kind: "current_user", ref: "" }],
      },
    } as unknown as CoworkProvenanceDetermination;

    expect(coworkProvenanceDeterminationIssue(unbound)).toBe(
      "Co-work couldn’t bind the author to the current identity.",
    );
  });
});
