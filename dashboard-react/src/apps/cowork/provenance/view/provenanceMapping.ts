import type { QuoteAnchor } from "../../rail/contracts";
import type {
  ProvenanceAttestation, ProvenanceAuthorshipKind, ProvenanceContributor,
  ProvenanceReviewStatus, ProvenanceTarget, ProvenanceData,
} from "./contracts";

type JsonObject = Record<string, unknown>;

const object = (value: unknown, path: string): JsonObject => {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`Invalid provenance view: ${path} must be an object.`);
  }
  return value as JsonObject;
};

const array = (value: unknown, path: string): readonly unknown[] => {
  if (!Array.isArray(value)) {
    throw new Error(`Invalid provenance view: ${path} must be an array.`);
  }
  return value;
};

const text = (value: unknown, path: string): string => {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`Invalid provenance view: ${path} must be a non-empty string.`);
  }
  return value;
};

const nullableText = (value: unknown, path: string): string | null => {
  if (value === null || value === undefined) return null;
  return text(value, path);
};

const choice = <T extends string>(
  value: unknown,
  choices: readonly T[],
  path: string,
): T => {
  if (typeof value !== "string" || !choices.includes(value as T)) {
    throw new Error(
      `Invalid provenance view: ${path} must be one of ${choices.join(", ")}.`,
    );
  }
  return value as T;
};

const integer = (value: unknown, path: string): number => {
  if (!Number.isSafeInteger(value) || Number(value) < 0) {
    throw new Error(`Invalid provenance view: ${path} must be a non-negative integer.`);
  }
  return Number(value);
};

const contributor = (value: unknown, path: string): ProvenanceContributor => {
  const raw = object(value, path);
  const kind = choice(raw.kind, ["human"] as const, `${path}.kind`);
  const ref = nullableText(raw.ref, `${path}.ref`);
  const displayName = nullableText(raw.display_name, `${path}.display_name`);
  const identityStatus = choice(
    raw.identity_status,
    ["local_actor_ref", "account_ref", "claimed_name"] as const,
    `${path}.identity_status`,
  );
  const label = displayName ?? ref;
  if (label === null) {
    throw new Error(`Invalid provenance view: ${path} must name a person.`);
  }
  return { kind, ref, label, identityStatus };
};

const quoteAnchor = (value: unknown, path: string): QuoteAnchor => {
  const raw = object(value, path);
  return {
    exact: text(raw.exact, `${path}.exact`),
    prefix: typeof raw.prefix === "string" ? raw.prefix : "",
    suffix: typeof raw.suffix === "string" ? raw.suffix : "",
  };
};

const attestation = (
  value: unknown,
  path: string,
): ProvenanceAttestation => {
  const raw = object(value, path);
  const assertedBy = object(raw.asserted_by, `${path}.asserted_by`);
  const scope = object(raw.scope, `${path}.scope`);
  const authorship = object(raw.authorship, `${path}.authorship`);
  const review = object(raw.human_review, `${path}.human_review`);
  const basis = object(raw.basis, `${path}.basis`);
  const source = object(raw.source, `${path}.source`);
  text(source.kind, `${path}.source.kind`);
  const meta = assertedBy.meta;
  if (
    meta !== null &&
    meta !== undefined &&
    (typeof meta !== "object" || Array.isArray(meta))
  ) {
    throw new Error(`Invalid provenance view: ${path}.asserted_by.meta must be an object.`);
  }
  return {
    attestationId: text(raw.attestation_id, `${path}.attestation_id`),
    at: text(raw.at, `${path}.at`),
    assertedBy: {
      kind: text(assertedBy.kind, `${path}.asserted_by.kind`),
      ref: nullableText(assertedBy.ref, `${path}.asserted_by.ref`),
      meta: (meta ?? null) as Readonly<Record<string, unknown>> | null,
    },
    scope: {
      kind: choice(
        scope.kind,
        ["document_version", "document_span"] as const,
        `${path}.scope.kind`,
      ),
      documentVersionId: nullableText(
        scope.document_version_id,
        `${path}.scope.document_version_id`,
      ),
      documentSpanId: nullableText(
        scope.document_span_id,
        `${path}.scope.document_span_id`,
      ),
      structuredHeadSha256: text(
        scope.structured_head_sha256,
        `${path}.scope.structured_head_sha256`,
      ),
    },
    authorship: {
      kind: choice(
        authorship.kind,
        ["human", "ai", "mixed", "unknown"] as const,
        `${path}.authorship.kind`,
      ) as ProvenanceAuthorshipKind,
      contributors: array(
        authorship.contributors,
        `${path}.authorship.contributors`,
      ).map((item, index) =>
        contributor(item, `${path}.authorship.contributors[${String(index)}]`),
      ),
    },
    humanReview: {
      status: choice(
        review.status,
        ["reviewed", "not_reviewed", "not_applicable", "unknown"] as const,
        `${path}.human_review.status`,
      ) as ProvenanceReviewStatus,
      reviewers: array(review.reviewers, `${path}.human_review.reviewers`).map(
        (item, index) =>
          contributor(item, `${path}.human_review.reviewers[${String(index)}]`),
      ),
    },
    source,
    basis: {
      kind: text(basis.kind, `${path}.basis.kind`),
      ref: nullableText(basis.ref, `${path}.basis.ref`),
    },
    supersedesId: nullableText(raw.supersedes_id, `${path}.supersedes_id`),
    canonicalSha256: text(raw.canonical_sha256, `${path}.canonical_sha256`),
  };
};

const target = (value: unknown, path: string): ProvenanceTarget => {
  const raw = object(value, path);
  const rawTarget = object(raw.target, `${path}.target`);
  const effectiveAttestations = array(
    raw.effective_attestations,
    `${path}.effective_attestations`,
  ).map((item, index) =>
    attestation(item, `${path}.effective_attestations[${String(index)}]`),
  );
  const resolution = choice(
    raw.resolution,
    ["resolved", "conflicted"] as const,
    `${path}.resolution`,
  );
  const effective =
    raw.effective_attestation === null
      ? null
      : attestation(raw.effective_attestation, `${path}.effective_attestation`);
  if (
    (resolution === "resolved" &&
      (effectiveAttestations.length !== 1 || effective === null)) ||
    (resolution === "conflicted" &&
      (effectiveAttestations.length < 1 || effective !== null))
  ) {
    throw new Error(`Invalid provenance view: ${path} has inconsistent resolution.`);
  }
  if (
    effective !== null &&
    !effectiveAttestations.some(
      (candidate) => candidate.attestationId === effective.attestationId,
    )
  ) {
    throw new Error(`Invalid provenance view: ${path} effective record is not a leaf.`);
  }
  const issue = raw.issue === null ? null : object(raw.issue, `${path}.issue`);
  const kind = choice(
    rawTarget.kind,
    ["document_version", "document_span"] as const,
    `${path}.target.kind`,
  );
  const documentVersionId = nullableText(rawTarget.document_version_id, `${path}.target.document_version_id`);
  const documentSpanId = nullableText(rawTarget.document_span_id, `${path}.target.document_span_id`);
  const span = raw.span === null ? null : quoteAnchor(raw.span, `${path}.span`);
  const currentness = choice(
    rawTarget.currentness,
    ["current", "stale", "requires_reanchor", "unavailable"] as const,
    `${path}.target.currentness`,
  );
  const reviewEligibility = choice(
    raw.review_eligibility,
    ["eligible", "stale_target", "conflicted", "not_ai_authored", "already_reviewed", "not_applicable"] as const,
    `${path}.review_eligibility`,
  );
  const inspectableMissingSpan =
    kind === "document_span" &&
    documentSpanId !== null &&
    documentVersionId === null &&
    span === null &&
    currentness === "unavailable" &&
    resolution === "conflicted" &&
    reviewEligibility === "conflicted" &&
    issue !== null;
  if (
    (kind === "document_span" &&
      (documentSpanId === null ||
        documentVersionId !== null ||
        (span === null && !inspectableMissingSpan))) ||
    (kind === "document_version" && (documentSpanId !== null || span !== null))
  ) {
    throw new Error(`Invalid provenance view: ${path} target identity is inconsistent.`);
  }
  return {
    projectionId: text(raw.projection_id, `${path}.projection_id`),
    target: {
      kind,
      documentVersionId,
      documentSpanId,
      structuredHeadSha256: text(
        rawTarget.structured_head_sha256,
        `${path}.target.structured_head_sha256`,
      ),
      currentness,
    },
    span,
    effectiveAttestations,
    effectiveAttestation: effective,
    resolution,
    reviewEligibility,
    issue:
      issue === null
        ? null
        : {
            code: text(issue.code, `${path}.issue.code`),
            message: text(issue.message, `${path}.issue.message`),
          },
    history: array(raw.history, `${path}.history`).map((item, index) =>
      attestation(item, `${path}.history[${String(index)}]`),
    ),
  };
};

/** Parse the additive wire projection before it reaches rail or editor rendering. */
export function mapProvenanceView(value: unknown): ProvenanceData {
  const raw = object(value, "provenance");
  if (raw.schema !== "cowork-provenance-view/v1") {
    throw new Error("Invalid provenance view: unsupported schema.");
  }
  const summary = object(raw.summary, "provenance.summary");
  if (typeof summary.unrecorded !== "boolean") {
    throw new Error("Invalid provenance view: provenance.summary.unrecorded must be boolean.");
  }
  return {
    schema: "cowork-provenance-view/v1",
    currentStructuredHeadSha256: nullableText(
      raw.current_structured_head_sha256,
      "provenance.current_structured_head_sha256",
    ),
    documentDefault:
      raw.document_default === null
        ? null
        : target(raw.document_default, "provenance.document_default"),
    spans: array(raw.spans, "provenance.spans").map((item, index) =>
      target(item, `provenance.spans[${String(index)}]`),
    ),
    history: array(raw.history, "provenance.history").map((item, index) =>
      attestation(item, `provenance.history[${String(index)}]`),
    ),
    summary: {
      totalTargets: integer(summary.total_targets, "provenance.summary.total_targets"),
      currentSpanCount: integer(
        summary.current_span_count,
        "provenance.summary.current_span_count",
      ),
      aiUnreviewedCount: integer(
        summary.ai_unreviewed_count,
        "provenance.summary.ai_unreviewed_count",
      ),
      reviewedCount: integer(
        summary.reviewed_count,
        "provenance.summary.reviewed_count",
      ),
      conflictedCount: integer(
        summary.conflicted_count,
        "provenance.summary.conflicted_count",
      ),
      staleCount: integer(summary.stale_count, "provenance.summary.stale_count"),
      unrecorded: summary.unrecorded,
    },
  };
}
