import type {
  ProvenanceAttestation,
  ProvenanceContributor,
  ProvenanceIdentityStatus,
} from "./contracts";

const canonicalize = (value: unknown): unknown => {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (typeof value !== "object" || value === null) return value;
  return Object.fromEntries(
    Object.entries(value)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, canonicalize(item)]),
  );
};

const fingerprint = (value: unknown): string =>
  JSON.stringify(canonicalize(value));

/** Semantic display axes; attester/basis may differ while the assertion agrees. */
export const provenanceAuthorshipFingerprint = (
  record: ProvenanceAttestation,
): string => fingerprint(record.authorship);

export const provenanceReviewFingerprint = (
  record: ProvenanceAttestation,
): string => fingerprint(record.humanReview);

export const provenanceSourceFingerprint = (
  record: ProvenanceAttestation,
): string => fingerprint(record.source);

export const provenanceDisplayedAxesFingerprint = (
  record: ProvenanceAttestation,
): string =>
  JSON.stringify([
    provenanceAuthorshipFingerprint(record),
    provenanceReviewFingerprint(record),
    provenanceSourceFingerprint(record),
  ]);

const boundedText = (value: unknown, maximum = 160): string | null => {
  if (typeof value !== "string") return null;
  const normalized = value.trim().replace(/[\u0000-\u001f\u007f]/gu, " ");
  if (normalized.length === 0) return null;
  return normalized.length <= maximum
    ? normalized
    : `${normalized.slice(0, maximum - 1)}…`;
};

const SOURCE_DETAIL_FIELDS = [
  ["label", "Label"],
  ["provider", "Provider"],
  ["model", "Model"],
  ["format", "Format"],
  ["media_type", "Media type"],
  ["path", "Path"],
  ["ref", "Reference"],
  ["surface", "Surface"],
  ["sha256", "Digest"],
  ["proposal_id", "Proposal"],
  ["acceptance_gesture_id", "Acceptance gesture"],
] as const;

const PRODUCER_DETAIL_FIELDS = [
  ["model", "Producer model"],
  ["harness", "Producer harness"],
  ["surface", "Producer surface"],
  ["session_id", "Producer run"],
] as const;

/** Whitelisted, bounded source fields only; never dumps arbitrary source metadata. */
export const provenanceSourceDetails = (
  record: ProvenanceAttestation,
): readonly { readonly label: string; readonly value: string }[] => {
  const direct = SOURCE_DETAIL_FIELDS.flatMap(([field, label]) => {
    const value = boundedText(record.source[field]);
    return value === null ? [] : [{ label, value }];
  });
  const producer = record.source.producer;
  const nested =
    typeof producer === "object" &&
    producer !== null &&
    !Array.isArray(producer)
      ? PRODUCER_DETAIL_FIELDS.flatMap(([field, label]) => {
          const value = boundedText(
            (producer as Readonly<Record<string, unknown>>)[field],
          );
          return value === null ? [] : [{ label, value }];
        })
      : [];
  return [...direct, ...nested];
};

export const provenanceIdentityStatusLabel = (
  status: ProvenanceIdentityStatus,
): string => {
  if (status === "account_ref") return "account-linked identity";
  if (status === "local_actor_ref") return "enrolled local user";
  return "claimed name; not account-verified";
};

export const provenancePersonDetail = (person: ProvenanceContributor): string =>
  `${person.label ?? person.ref ?? "Unnamed person"} (${provenanceIdentityStatusLabel(person.identityStatus)})`;
