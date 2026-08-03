export const COWORK_PROVENANCE_DETERMINATION_SCHEMA =
  "cowork-authorship-attestation/v1" as const;

export type CoworkProvenanceAuthorshipKind =
  | "human"
  | "ai"
  | "mixed"
  | "unknown";

export type CoworkProvenanceReviewStatus =
  | "reviewed"
  | "not_reviewed"
  | "not_applicable";

export type CoworkProvenanceIdentityStatus =
  | "local_actor_ref"
  | "account_ref";

/**
 * The server-resolved identity in force when a provenance gesture begins.
 * It is captured into every "Me" person so delayed delivery cannot resolve
 * against a different user after an account switch.
 */
export interface CoworkProvenanceActorIdentity {
  readonly kind: "human";
  readonly ref: string;
  readonly identity_status: CoworkProvenanceIdentityStatus;
}

/**
 * "Me" is bound to the immutable actor/account ref captured for this gesture.
 * A typed name is a claim rather than an authenticated account identity.
 */
export type CoworkProvenancePerson =
  | {
      readonly kind: "current_user";
      readonly ref: string;
      readonly identity_status: CoworkProvenanceIdentityStatus;
    }
  | {
      readonly kind: "named_person";
      /**
       * A typed name is a user claim, not an authenticated account identity.
       * This wire name intentionally matches the Co-work attestation API.
       */
      readonly display_name: string;
    };

export interface CoworkProvenanceAuthorship {
  readonly kind: CoworkProvenanceAuthorshipKind;
  /**
   * Human and mixed authorship may name more than one contributor even though
   * the first UI slice edits only the primary person.
   */
  readonly contributors: readonly CoworkProvenancePerson[];
}

export type CoworkProvenanceReview =
  | {
      readonly status: "reviewed";
      readonly reviewers: readonly CoworkProvenancePerson[];
    }
  | {
      readonly status: "not_reviewed" | "not_applicable";
      readonly reviewers: readonly [];
    };

export interface CoworkProvenanceDetermination {
  readonly schema: typeof COWORK_PROVENANCE_DETERMINATION_SCHEMA;
  readonly authorship: CoworkProvenanceAuthorship;
  /**
   * Snake case makes this controlled value directly usable as the
   * `authorship_attestation` API payload, without an untyped translation.
   */
  readonly human_review: CoworkProvenanceReview;
}

export const currentCoworkUser = (
  identity: CoworkProvenanceActorIdentity,
): CoworkProvenancePerson => ({
  kind: "current_user",
  ref: identity.ref,
  identity_status: identity.identity_status,
});

export const defaultCoworkProvenanceDetermination =
  (
    identity: CoworkProvenanceActorIdentity,
  ): CoworkProvenanceDetermination => ({
    schema: COWORK_PROVENANCE_DETERMINATION_SCHEMA,
    authorship: {
      kind: "human",
      contributors: [currentCoworkUser(identity)],
    },
    human_review: {
      status: "not_applicable",
      reviewers: [],
    },
  });

/** Honest fallback when the user defers a provenance determination. */
export const unknownCoworkProvenanceDetermination =
  (): CoworkProvenanceDetermination => ({
    schema: COWORK_PROVENANCE_DETERMINATION_SCHEMA,
    authorship: {
      kind: "unknown",
      contributors: [],
    },
    human_review: {
      status: "not_applicable",
      reviewers: [],
    },
  });

const namedPersonIssue = (
  people: readonly CoworkProvenancePerson[],
  label: string,
): string | null => {
  const unnamed = people.some(
    (person) =>
      person.kind === "named_person" &&
      person.display_name.trim().length === 0,
  );
  if (unnamed) return `Enter the ${label}’s name.`;
  const unbound = people.some(
    (person) =>
      person.kind === "current_user" &&
      (person.ref.trim().length === 0 ||
        (person.identity_status !== "local_actor_ref" &&
          person.identity_status !== "account_ref")),
  );
  return unbound
    ? `Co-work couldn’t bind the ${label} to the current identity.`
    : null;
};

/**
 * Return one concise user-facing issue, or null when the controlled value can
 * be submitted. The server remains authoritative for durable person identity.
 */
export const coworkProvenanceDeterminationIssue = (
  value: CoworkProvenanceDetermination,
): string | null => {
  if (value.schema !== COWORK_PROVENANCE_DETERMINATION_SCHEMA) {
    return "This provenance determination uses an unsupported format.";
  }

  const { kind, contributors } = value.authorship;
  if (
    (kind === "human" || kind === "mixed") &&
    contributors.length === 0
  ) {
    return "Choose who contributed the human-written text.";
  }
  if ((kind === "ai" || kind === "unknown") && contributors.length > 0) {
    return "AI or unknown authorship cannot carry a human author.";
  }

  const authorIssue = namedPersonIssue(contributors, "author");
  if (authorIssue !== null) return authorIssue;

  if (kind === "human" && value.human_review.status !== "not_applicable") {
    return "Human review does not apply to text recorded as human-written.";
  }
  if (kind === "unknown" && value.human_review.status !== "not_applicable") {
    return "Human review does not apply when authorship is unknown.";
  }
  if (
    (kind === "ai" || kind === "mixed") &&
    value.human_review.status === "not_applicable"
  ) {
    return "Choose the human-review status for AI-written text.";
  }

  if (value.human_review.status === "reviewed") {
    if (value.human_review.reviewers.length === 0) {
      return "Choose who reviewed the text.";
    }
    return namedPersonIssue(value.human_review.reviewers, "reviewer");
  }
  return value.human_review.reviewers.length === 0
    ? null
    : "Only reviewed text can name a reviewer.";
};
