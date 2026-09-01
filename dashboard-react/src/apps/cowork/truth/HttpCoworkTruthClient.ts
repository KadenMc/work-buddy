import type {
  TruthClaimBaseStatus,
  TruthClaimDecisionRequest,
  TruthClaimDetail,
  TruthClaimFilter,
  TruthClaimHealth,
  TruthClaimSummary,
  TruthClaimAction,
  TruthClaimsSnapshot,
  TruthConnectClaimRequest,
  TruthDerivation,
  TruthDerivationPremise,
  TruthEvidenceReceipt,
  TruthExpressionRole,
  TruthFilterCounts,
  TruthInvalidationListener,
  TruthLifecycleEvent,
  TruthMutationReceipt,
  TruthPassageConnection,
  TruthProposeClaimRequest,
  TruthQuery,
  TruthQuoteSelector,
  TruthRailProvider,
  TruthRecordProvenance,
  TruthPremiseAssessment,
  TruthSupportAssessment,
  TruthUnsubscribe,
  TruthViewScope,
  TruthConflict,
} from "./contracts";
import type {
  CoworkDocumentCapabilityEnvelope,
  CoworkTruthActivation,
  CoworkTruthEligibility,
} from "../contracts";
import {
  initializeLocalIdentity,
  issueHumanGesture,
  localIdentityHeaders,
  sha256Hex,
} from "../../../security/localIdentity";

type JsonObject = Record<string, unknown>;

const canonicalJson = (value: unknown): string => {
  if (value === null) return "null";
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (typeof value === "object") {
    const entries = Object.entries(value as JsonObject)
      .filter(([, item]) => item !== undefined)
      .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0));
    return `{${entries
      .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
};

const TRUTH_MUTATION_GESTURE_SCHEMA = "wb.cowork.truth-mutation-gesture/v1";
const TRUTH_PROPOSE_ACTION = "cowork.truth.propose_and_connect";
const TRUTH_CONNECT_ACTION = "cowork.truth.connect";
const TRUTH_LIFECYCLE_ACTION = "cowork.truth.claim_decision";
const TRUTH_ACTIVATION_ACTION = "cowork.truth.activation.change";

const objectValue = (value: unknown): JsonObject =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as JsonObject)
    : {};

const first = (value: JsonObject, ...keys: readonly string[]): unknown => {
  for (const key of keys) {
    if (value[key] !== undefined) return value[key];
  }
  return undefined;
};

const stringValue = (value: unknown, fallback = ""): string =>
  typeof value === "string" ? value : fallback;
const nullableString = (value: unknown): string | null =>
  typeof value === "string" ? value : null;
const booleanValue = (value: unknown, fallback = false): boolean =>
  typeof value === "boolean" ? value : fallback;
const numberValue = (value: unknown, fallback = 0): number =>
  typeof value === "number" && Number.isFinite(value) ? value : fallback;
const arrayValue = (value: unknown): readonly unknown[] =>
  Array.isArray(value) ? value : [];
const stringArray = (value: unknown): readonly string[] =>
  arrayValue(value).filter((item): item is string => typeof item === "string");

const BASE_STATUSES = new Set<TruthClaimBaseStatus>([
  "proposed",
  "confirmed",
  "rejected",
  "expired",
  "challenged",
  "superseded",
  "retracted",
  "unknown",
]);
const HEALTH_STATES = new Set<TruthClaimHealth>([
  "clean",
  "needs_review",
  "conflict",
  "failed",
  "redacted",
  "voided",
  "unknown",
]);
const ROLES = new Set<TruthExpressionRole>([
  "quote",
  "paraphrase",
  "summary",
  "instantiation",
]);
const ACTIONS = new Set<TruthClaimAction>([
  "confirm",
  "reaffirm",
  "reject",
  "challenge",
  "supersede",
  "redact",
]);

const baseStatus = (value: unknown): TruthClaimBaseStatus =>
  typeof value === "string" && BASE_STATUSES.has(value as TruthClaimBaseStatus)
    ? (value as TruthClaimBaseStatus)
    : "unknown";
const healthState = (value: unknown): TruthClaimHealth =>
  value === "conflicted"
    ? "conflict"
    : value === "invalid"
      ? "failed"
      : typeof value === "string" && HEALTH_STATES.has(value as TruthClaimHealth)
        ? (value as TruthClaimHealth)
        : "unknown";
const expressionRole = (value: unknown): TruthExpressionRole =>
  typeof value === "string" && ROLES.has(value as TruthExpressionRole)
    ? (value as TruthExpressionRole)
    : "instantiation";
const claimActions = (value: unknown): readonly TruthClaimAction[] =>
  arrayValue(value).filter(
    (item): item is TruthClaimAction =>
      typeof item === "string" && ACTIONS.has(item as TruthClaimAction),
  );

const quoteSelector = (raw: unknown, fallbackQuote = ""): TruthQuoteSelector => {
  const value = objectValue(raw);
  const exact = stringValue(first(value, "exact"), fallbackQuote);
  const start = first(value, "start");
  const end = first(value, "end");
  return {
    kind: "text_quote",
    exact,
    prefix: stringValue(first(value, "prefix")),
    suffix: stringValue(first(value, "suffix")),
    ...(typeof start === "number" ? { start } : {}),
    ...(typeof end === "number" ? { end } : {}),
  };
};

const recordProvenance = (
  raw: unknown,
  fallbackCreator: JsonObject,
  fallbackAt: string,
): TruthRecordProvenance => {
  if (raw === undefined || raw === null) {
    return {
      preparedBy: null,
      addedBy: {
        kind: stringValue(first(fallbackCreator, "kind"), "unknown"),
        ref: nullableString(first(fallbackCreator, "ref")),
        at: fallbackAt,
      },
    };
  }
  const value = objectValue(raw);
  const rawPrepared = first(value, "prepared_by", "preparedBy");
  const rawAdded = first(value, "added_by", "addedBy");
  const added = objectValue(rawAdded);
  const addedKind = stringValue(first(added, "kind"));
  const addedAt = stringValue(first(added, "at"));
  if (rawAdded === undefined || addedKind.length === 0 || addedAt.length === 0) {
    throw new Error("Truth returned invalid record provenance.");
  }
  if (rawPrepared === null) {
    return {
      preparedBy: null,
      addedBy: {
        kind: addedKind,
        ref: nullableString(first(added, "ref")),
        at: addedAt,
      },
    };
  }
  const prepared = objectValue(rawPrepared);
  const analysisRunId = stringValue(first(prepared, "analysis_run_id", "analysisRunId"));
  const candidateId = stringValue(first(prepared, "candidate_id", "candidateId"));
  const providerId = stringValue(first(prepared, "provider_id", "providerId"));
  const modelId = stringValue(first(prepared, "model_id", "modelId"));
  if (
    first(prepared, "kind") !== "agent_run" ||
    first(prepared, "surface") !== "cowork_truth_analysis" ||
    analysisRunId.length === 0 ||
    candidateId.length === 0 ||
    providerId.length === 0 ||
    modelId.length === 0
  ) {
    throw new Error("Truth returned invalid preparation provenance.");
  }
  return {
    preparedBy: {
      kind: "agent_run",
      surface: "cowork_truth_analysis",
      analysisRunId,
      candidateId,
      providerId,
      modelId,
    },
    addedBy: {
      kind: addedKind,
      ref: nullableString(first(added, "ref")),
      at: addedAt,
    },
  };
};

const passageConnection = (
  raw: unknown,
  currentDocumentId: string,
): TruthPassageConnection => {
  const value = objectValue(raw);
  const quote = stringValue(first(value, "quote", "quote_exact"));
  const documentId = stringValue(
    first(value, "document_id", "documentId"),
    currentDocumentId,
  );
  const rawCreator = first(value, "created_by", "createdBy");
  const creator = objectValue(rawCreator);
  const createdAt = stringValue(first(value, "created_at", "createdAt"));
  return {
    expressionId: stringValue(first(value, "expression_id", "expressionId")),
    spanId: stringValue(first(value, "span_id", "spanId")),
    documentId,
    documentTitle: nullableString(
      first(value, "document_title", "documentTitle", "title"),
    ),
    documentPath: nullableString(first(value, "document_path", "documentPath")),
    role: expressionRole(first(value, "role")),
    quote,
    selector: quoteSelector(
      first(value, "selector", "quote_anchor", "quoteAnchor"),
      quote,
    ),
    currentDocument: booleanValue(
      first(value, "current_document", "currentDocument"),
      documentId === currentDocumentId,
    ),
    claimCanonicalSha256: stringValue(
      first(value, "claim_canonical_sha256", "claimCanonicalSha256"),
    ),
    createdAt,
    createdBy:
      rawCreator === undefined || rawCreator === null
        ? null
        : {
            kind: stringValue(first(creator, "kind"), "unknown"),
            ref: nullableString(first(creator, "ref")),
          },
    provenance: recordProvenance(first(value, "provenance"), creator, createdAt),
  };
};

const claimSummary = (
  raw: unknown,
  currentDocumentId: string,
): TruthClaimSummary => {
  const value = objectValue(raw);
  const rawConnections = arrayValue(
    first(value, "document_connections", "documentConnections", "connections", "expressions"),
  );
  const connections = rawConnections.map((item) =>
    passageConnection(item, currentDocumentId),
  );
  const status = baseStatus(
    first(value, "base_status", "baseStatus", "status"),
  );
  const needsReview = booleanValue(
    first(value, "needs_review", "needsReview"),
    stringValue(first(value, "status")) === "needs_review",
  );
  const health = healthState(first(value, "health"));
  const voided = booleanValue(first(value, "voided"));
  const redacted = booleanValue(
    first(value, "redacted"),
    nullableString(first(value, "redacted_at", "redactedAt")) !== null,
  );
  const serverFact = first(value, "is_fact", "isFact");
  const rawCreator = first(value, "created_by", "createdBy");
  const creator = objectValue(rawCreator);
  const createdAt = stringValue(first(value, "created_at", "createdAt"));
  return {
    claimId: stringValue(first(value, "claim_id", "claimId", "id")),
    proposition: stringValue(first(value, "proposition"), "[redacted claim]"),
    claimKind: stringValue(
      first(value, "claim_kind", "claimKind"),
      "claim",
    ),
    canonicalSha256: stringValue(
      first(value, "canonical_sha256", "canonicalSha256"),
    ),
    scope: stringValue(first(value, "scope"), "store"),
    baseStatus: status,
    needsReview,
    health,
    healthReason: nullableString(
      first(value, "health_reason", "healthReason"),
    ),
    voided,
    redacted,
    validFrom: nullableString(first(value, "valid_from", "validFrom")),
    validTo: nullableString(first(value, "valid_to", "validTo")),
    effectiveValidFrom: nullableString(
      first(value, "effective_valid_from", "effectiveValidFrom", "valid_from"),
    ),
    effectiveValidTo: nullableString(
      first(value, "effective_valid_to", "effectiveValidTo", "valid_to"),
    ),
    evidenceCount: numberValue(
      first(value, "evidence_count", "evidenceCount", "receipt_count"),
      arrayValue(first(value, "receipts", "evidence")).length,
    ),
    connectionCount: numberValue(
      first(value, "connection_count", "connectionCount", "expression_count"),
      connections.length,
    ),
    connections,
    createdAt,
    createdBy:
      rawCreator === undefined || rawCreator === null
        ? null
        : {
            kind: stringValue(first(creator, "kind"), "unknown"),
            ref: nullableString(first(creator, "ref")),
          },
    provenance: recordProvenance(first(value, "provenance"), creator, createdAt),
    isFact:
      typeof serverFact === "boolean"
        ? serverFact
        : false,
    availableActions: claimActions(
      first(value, "available_actions", "availableActions"),
    ),
  };
};

const evidenceReceipt = (raw: unknown): TruthEvidenceReceipt => {
  const value = objectValue(raw);
  const author = objectValue(first(value, "author"));
  const rawIntegrity = first(value, "integrity");
  const integrity = objectValue(rawIntegrity);
  return {
    linkId: stringValue(first(value, "link_id", "linkId")),
    spanId: stringValue(first(value, "span_id", "spanId")),
    evidenceId: stringValue(first(value, "evidence_id", "evidenceId")),
    evidenceKind: stringValue(
      first(value, "evidence_kind", "evidenceKind", "kind"),
      "evidence",
    ),
    quote: nullableString(first(value, "quote", "quote_exact")),
    sourceLocator: stringValue(
      first(value, "source_locator", "sourceLocator"),
      "Unknown source",
    ),
    trustClass: stringValue(
      first(value, "trust_class", "trustClass"),
      "unattested",
    ),
    authorKind: nullableString(
      first(value, "author_kind", "authorKind") ?? first(author, "kind"),
    ),
    authorRef: nullableString(
      first(value, "author_ref", "authorRef") ?? first(author, "ref"),
    ),
    active: booleanValue(first(value, "active"), true),
    spanSha256: stringValue(first(value, "span_sha256", "spanSha256")),
    contentSha256: stringValue(
      first(value, "content_sha256", "contentSha256"),
    ),
    mediaType: nullableString(first(value, "media_type", "mediaType")),
    derivedFromStore: nullableString(
      first(value, "derived_from_store", "derivedFromStore"),
    ),
    acquiredAt: nullableString(first(value, "acquired_at", "acquiredAt")),
    acquisitionMethod: nullableString(
      first(value, "acquisition_method", "acquisitionMethod"),
    ),
    spanRedactedAt: nullableString(
      first(value, "span_redacted_at", "spanRedactedAt"),
    ),
    evidenceRedactedAt: nullableString(
      first(value, "evidence_redacted_at", "evidenceRedactedAt"),
    ),
    integrity:
      rawIntegrity === undefined || rawIntegrity === null
        ? null
        : {
            state: stringValue(first(integrity, "state"), "unknown"),
            detail: nullableString(first(integrity, "detail")),
            locatorScheme: nullableString(
              first(integrity, "locator_scheme", "locatorScheme"),
            ),
            verifiabilityClass: nullableString(
              first(
                integrity,
                "verifiability_class",
                "verifiabilityClass",
              ),
            ),
            snapshotPresent: booleanValue(
              first(integrity, "snapshot_present", "snapshotPresent"),
            ),
          },
  };
};

const lifecycleEvent = (raw: unknown): TruthLifecycleEvent => {
  const value = objectValue(raw);
  return {
    eventId: stringValue(first(value, "event_id", "eventId", "id")),
    status: stringValue(first(value, "status"), "unknown"),
    at: stringValue(first(value, "at", "created_at", "createdAt")),
    actorKind: stringValue(first(value, "actor_kind", "actorKind"), "unknown"),
    actorRef: nullableString(first(value, "actor_ref", "actorRef")),
    note: nullableString(first(value, "note", "reason")),
  };
};

const conflict = (raw: unknown): TruthConflict => {
  const value = objectValue(raw);
  return {
    relationId: stringValue(first(value, "relation_id", "relationId", "link_id")),
    claimId: stringValue(first(value, "claim_id", "claimId", "to_claim_id")),
    proposition: nullableString(first(value, "proposition")),
    status: nullableString(first(value, "status")),
    conflictType: nullableString(
      first(value, "conflict_type", "conflictType", "relation"),
    ),
    conflictClass: nullableString(
      first(value, "conflict_class", "conflictClass"),
    ),
    direction:
      first(value, "direction") === "challenges"
        ? "challenges"
        : first(value, "direction") === "challenged_by"
          ? "challenged_by"
          : "unknown",
    createdAt: nullableString(first(value, "created_at", "createdAt")),
  };
};

const supportAssessment = (raw: unknown): TruthSupportAssessment => {
  const value = objectValue(raw);
  return {
    supportSpanIds: stringArray(
      first(value, "support_span_ids", "supportSpanIds"),
    ),
    usableSpanIds: stringArray(
      first(value, "usable_span_ids", "usableSpanIds"),
    ),
    quarantinedOnly: booleanValue(
      first(value, "quarantined_only", "quarantinedOnly"),
    ),
    agentAuthoredOnly: booleanValue(
      first(value, "agent_authored_only", "agentAuthoredOnly"),
    ),
    storeDerivedOnly: booleanValue(
      first(value, "store_derived_only", "storeDerivedOnly"),
    ),
  };
};

const premiseAssessment = (raw: unknown): TruthPremiseAssessment => {
  const value = objectValue(raw);
  return {
    localUnconfirmed: stringArray(
      first(value, "local_unconfirmed", "localUnconfirmed"),
    ),
    unresolvedUris: stringArray(
      first(value, "unresolved_uris", "unresolvedUris"),
    ),
    confirmed: booleanValue(first(value, "confirmed")),
  };
};

const premise = (raw: unknown): TruthDerivationPremise => {
  const value = objectValue(raw);
  return {
    kind: stringValue(first(value, "kind", "premise_kind"), "claim"),
    ref: stringValue(first(value, "ref", "premise_ref")),
    proposition: nullableString(first(value, "proposition")),
    status: nullableString(first(value, "status")),
  };
};

const derivation = (raw: unknown): TruthDerivation => {
  const value = objectValue(raw);
  return {
    method: stringValue(first(value, "method"), "derived"),
    rationale: nullableString(first(value, "rationale")),
    confidence:
      typeof first(value, "confidence") === "number"
        ? numberValue(first(value, "confidence"))
        : null,
    premises: arrayValue(first(value, "premises")).map(premise),
  };
};

const detailFrom = (
  raw: unknown,
  currentDocumentId: string,
): TruthClaimDetail => {
  const value = objectValue(raw);
  const claimValue = objectValue(first(value, "claim", "detail"));
  const source = Object.keys(claimValue).length > 0 ? claimValue : value;
  const summary = claimSummary(source, currentDocumentId);
  const detailConnectionsRaw = first(value, "connections");
  const connections =
    detailConnectionsRaw === undefined
      ? summary.connections
      : arrayValue(detailConnectionsRaw).map((item) =>
          passageConnection(item, currentDocumentId),
        );
  const binding = objectValue(first(value, "decision_binding", "decisionBinding"));
  const bindingPayload = stringValue(
    first(binding, "payload_sha256", "payloadSha256"),
  );
  const bindingContext = stringValue(
    first(binding, "context_sha256", "contextSha256"),
  );
  return {
    ...summary,
    connections,
    connectionCount: Math.max(summary.connectionCount, connections.length),
    structured: objectValue(first(source, "structured", "structured_json")),
    receipts: arrayValue(first(value, "receipts", "evidence", "source_receipts")).map(
      evidenceReceipt,
    ),
    lifecycle: arrayValue(
      first(value, "status_history", "statusHistory", "lifecycle", "events", "status_events"),
    ).map(lifecycleEvent),
    conflicts: arrayValue(first(value, "conflicts")).map(conflict),
    derivations: arrayValue(first(value, "derivations")).map(derivation),
    support: supportAssessment(first(value, "support")),
    premises: premiseAssessment(first(value, "premises")),
    decisionBinding:
      bindingPayload.length === 0 || bindingContext.length === 0
        ? null
        : {
            payloadSha256: bindingPayload,
            contextSha256: bindingContext,
            agentAuthoredOnly: booleanValue(
              first(binding, "agent_authored_only", "agentAuthoredOnly"),
            ),
          },
  };
};

const countValue = (
  value: JsonObject,
  snake: string,
  camel: string,
  fallback: number,
): number => numberValue(first(value, snake, camel), fallback);

const countsFrom = (
  raw: unknown,
  claims: readonly TruthClaimSummary[],
): TruthFilterCounts => {
  const value = objectValue(raw);
  return {
    all: countValue(value, "all", "all", claims.length),
    facts: countValue(
      value,
      "facts",
      "facts",
      claims.filter((claim) => claim.isFact).length,
    ),
    proposed: countValue(
      value,
      "proposed",
      "proposed",
      claims.filter((claim) => claim.baseStatus === "proposed").length,
    ),
    needsReview: countValue(
      value,
      "needs_review",
      "needsReview",
      claims.filter((claim) => claim.needsReview).length,
    ),
    challenged: countValue(
      value,
      "challenged",
      "challenged",
      claims.filter((claim) => claim.baseStatus === "challenged").length,
    ),
    unconnected: countValue(
      value,
      "unconnected",
      "unconnected",
      claims.filter((claim) => claim.connectionCount === 0).length,
    ),
  };
};

const mutationReceipt = (raw: unknown): TruthMutationReceipt => {
  const value = objectValue(raw);
  return {
    ok: booleanValue(first(value, "ok"), true),
    claimId: nullableString(first(value, "claim_id", "claimId")),
    claimCreated: booleanValue(
      first(value, "claim_created", "claimCreated"),
    ),
    expressionId: nullableString(
      first(value, "expression_id", "expressionId"),
    ),
    expressionCreated: booleanValue(
      first(value, "expression_created", "expressionCreated"),
    ),
    status: nullableString(first(value, "status")),
  };
};

const truthActivationEnvelope = (
  raw: unknown,
): CoworkDocumentCapabilityEnvelope => {
  const envelope = objectValue(raw);
  const interaction = objectValue(
    first(envelope, "interaction_contract", "interactionContract"),
  );
  const modules = objectValue(first(envelope, "modules"));
  const truth = objectValue(first(envelope, "truth"));
  const rawEligibility = first(truth, "eligibility");
  const eligibility: CoworkTruthEligibility =
    rawEligibility === "allowed" || rawEligibility === "required"
      ? rawEligibility
      : "unsupported";
  const rawActivation = first(truth, "activation");
  const activation: CoworkTruthActivation | null =
    rawActivation === "disabled" ||
    rawActivation === "enabled" ||
    rawActivation === "paused"
      ? rawActivation
      : null;
  const rawRevision = first(truth, "activation_revision", "activationRevision");
  const activationRevision =
    activation !== null &&
    typeof rawRevision === "number" &&
    Number.isSafeInteger(rawRevision) &&
    rawRevision > 0
      ? rawRevision
      : null;
  const contractId = stringValue(
    first(interaction, "contract_id", "contractId", "id"),
  );
  const contractVersion = numberValue(first(interaction, "version"));
  const contractDigest = nullableString(
    first(interaction, "digest", "definition_sha256", "definitionSha256"),
  );
  if (
    first(envelope, "schema") !== "wb.cowork-document-capabilities/v1" ||
    contractId.length === 0 ||
    !Number.isSafeInteger(contractVersion) ||
    contractVersion < 1 ||
    contractDigest === null ||
    contractDigest.length !== 64 ||
    (eligibility !== "unsupported" &&
      (activation === null || activationRevision === null))
  ) {
    throw new Error("Truth returned an invalid activation policy.");
  }
  return {
    schema: "wb.cowork-document-capabilities/v1",
    interactionContract: {
      contractId,
      version: contractVersion,
      digest: contractDigest,
    },
    modules: {
      review: booleanValue(first(modules, "review")),
      provenance: booleanValue(first(modules, "provenance")),
      chat: booleanValue(first(modules, "chat")),
      truth: booleanValue(first(modules, "truth")),
    },
    truth: {
      eligibility,
      activation,
      activationRevision,
      policyFingerprint: nullableString(
        first(truth, "policy_fingerprint", "policyFingerprint"),
      ),
      ledgerPresent: booleanValue(
        first(truth, "ledger_present", "ledgerPresent"),
      ),
      unavailableReason: nullableString(
        first(truth, "unavailable_reason", "unavailableReason"),
      ),
    },
  };
};

const digestValue = (value: unknown, label: string): string => {
  const digest = stringValue(value).toLowerCase();
  if (digest.length !== 64 || !/^[0-9a-f]{64}$/.test(digest)) {
    throw new Error(`Truth returned an invalid ${label}.`);
  }
  return digest;
};

export interface TruthActivationPolicySnapshot {
  readonly capabilityEnvelope: CoworkDocumentCapabilityEnvelope;
  readonly documentHeadSha256: string;
}

export interface TruthActivationTransitionRequest {
  readonly nextState: CoworkTruthActivation;
  readonly expectedActivationRevision: number;
  readonly expectedInteractionContractSha256: string;
  readonly expectedDocumentHeadSha256: string;
  readonly intentId: string;
  readonly reason?: string;
}

const activationPolicySnapshot = (
  raw: unknown,
): TruthActivationPolicySnapshot => {
  const value = objectValue(raw);
  return {
    capabilityEnvelope: truthActivationEnvelope(
      first(value, "capability_envelope", "capabilityEnvelope"),
    ),
    documentHeadSha256: digestValue(
      first(value, "document_head_sha256", "documentHeadSha256"),
      "document head",
    ),
  };
};

const captureBody = (
  request: TruthProposeClaimRequest | TruthConnectClaimRequest,
): JsonObject => ({
  expected_structured_head_sha256: request.capture.structuredHeadSha256,
  expected_ydoc_generation_sha256: request.capture.ydocGenerationSha256,
  expected_projection_sha256: request.capture.projectionSha256,
  selector: request.capture.selector,
  role: request.role,
});

export interface HttpCoworkTruthClientOptions {
  readonly storeId: string;
  readonly documentId: string;
  readonly fetchImpl?: typeof fetch;
}

export class HttpCoworkTruthClient implements TruthRailProvider {
  readonly #storeId: string;
  readonly #documentId: string;
  readonly #fetch: typeof fetch;
  readonly #listeners = new Set<TruthInvalidationListener>();

  constructor(options: HttpCoworkTruthClientOptions) {
    this.#storeId = options.storeId;
    this.#documentId = options.documentId;
    this.#fetch = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  #base(): string {
    return `/api/truth/doc/${encodeURIComponent(this.#documentId)}/truth`;
  }

  #query(): string {
    return `store_id=${encodeURIComponent(this.#storeId)}`;
  }

  subscribe(listener: TruthInvalidationListener): TruthUnsubscribe {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  /** Surface/SSE adapters may fan an authoritative Truth nudge through here. */
  invalidate(): void {
    for (const listener of this.#listeners) listener();
  }

  async loadActivationPolicy(): Promise<TruthActivationPolicySnapshot> {
    return activationPolicySnapshot(
      await this.#json(`${this.#base()}/policy?${this.#query()}`),
    );
  }

  async transitionTruthActivation(
    request: TruthActivationTransitionRequest,
  ): Promise<TruthActivationPolicySnapshot> {
    const reason = request.reason?.trim() || null;
    const body = {
      next_state: request.nextState,
      expected_activation_revision: request.expectedActivationRevision,
      expected_interaction_contract_sha256:
        request.expectedInteractionContractSha256,
      expected_document_head_sha256: request.expectedDocumentHeadSha256,
      intent_id: request.intentId,
      reason,
    };
    const payload = await this.#authorizedJson({
      action: TRUTH_ACTIVATION_ACTION,
      operation: "activation",
      payload: body,
      url: `${this.#base()}/activation?${this.#query()}`,
      body,
    });
    this.invalidate();
    return activationPolicySnapshot(payload);
  }

  async #json(
    url: string,
    init: RequestInit = { method: "GET" },
  ): Promise<unknown> {
    const response = await this.#fetch(url, {
      credentials: "same-origin",
      ...init,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const value = objectValue(payload);
      const nestedError = objectValue(first(value, "error"));
      throw new Error(
        stringValue(
          first(nestedError, "message") ?? first(value, "message", "error"),
          "Truth could not complete that request.",
        ),
      );
    }
    return payload;
  }

  async #authorizedJson(input: {
    readonly action: string;
    readonly operation: string;
    readonly claimId?: string;
    readonly payload: JsonObject;
    readonly url: string;
    readonly body: JsonObject;
  }): Promise<unknown> {
    const identity = await initializeLocalIdentity({ fetchImpl: this.#fetch });
    if (!identity.authenticated) {
      throw new Error(
        identity.reason || "An authenticated local session is required.",
      );
    }
    const contextSha256 = await sha256Hex(
      canonicalJson({
        schema: TRUTH_MUTATION_GESTURE_SCHEMA,
        operation: input.operation,
        store_id: this.#storeId,
        document_id: this.#documentId,
        payload: input.payload,
      }),
    );
    const subject = [
      "cowork-truth",
      input.operation,
      this.#storeId,
      this.#documentId,
      ...(input.claimId ? [input.claimId] : []),
    ].join(":");
    const gesture = await issueHumanGesture(
      { action: input.action, subject, contextSha256 },
      this.#fetch,
    );
    return this.#json(input.url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...localIdentityHeaders(gesture.token),
      },
      body: JSON.stringify(input.body),
    });
  }

  async load(query: TruthQuery): Promise<TruthClaimsSnapshot> {
    const claimsById = new Map<string, TruthClaimSummary>();
    const visitedOffsets = new Set<number>();
    let offset = 0;
    let payload: JsonObject | null = null;
    let nextOffset: number | null = 0;
    while (nextOffset !== null) {
      if (visitedOffsets.has(offset)) {
        throw new Error("Truth returned an invalid pagination cursor.");
      }
      visitedOffsets.add(offset);
      const params = new URLSearchParams({
        store_id: this.#storeId,
        view: query.scope,
        filter: query.filter,
        offset: String(offset),
        limit: "200",
      });
      const page = objectValue(
        await this.#json(`${this.#base()}?${params.toString()}`),
      );
      payload ??= page;
      for (const item of arrayValue(first(page, "claims", "items"))) {
        const claim = claimSummary(item, this.#documentId);
        if (claim.claimId.length > 0) claimsById.set(claim.claimId, claim);
      }
      const rawNext = first(page, "next_offset", "nextOffset");
      nextOffset =
        typeof rawNext === "number" && Number.isFinite(rawNext)
          ? rawNext
          : null;
      if (nextOffset !== null) {
        if (nextOffset <= offset) {
          throw new Error("Truth returned an invalid pagination cursor.");
        }
        offset = nextOffset;
      }
    }
    const firstPayload = payload ?? {};
    const claims = [...claimsById.values()];
    const rawCapabilities = objectValue(first(firstPayload, "capabilities"));
    const capabilities = {
      canObserve: booleanValue(
        first(rawCapabilities, "can_observe", "canObserve"),
        true,
      ),
      canModify: booleanValue(
        first(rawCapabilities, "can_modify", "canModify"),
      ),
      canDecide: booleanValue(
        first(rawCapabilities, "can_decide", "canDecide"),
      ),
      allowedClaimKinds: arrayValue(
        first(rawCapabilities, "allowed_claim_kinds", "allowedClaimKinds"),
      ).filter((item): item is string => typeof item === "string"),
      mutationUnavailableReason: nullableString(
        first(
          rawCapabilities,
          "mutation_unavailable_reason",
          "mutationUnavailableReason",
        ),
      ),
    };
    const rawScope = stringValue(first(firstPayload, "view", "scope"), query.scope);
    const rawFilter = stringValue(first(firstPayload, "filter"), query.filter);
    return {
      schema: stringValue(first(firstPayload, "schema"), "wb.cowork.truth/v1"),
      storeId: stringValue(
        first(firstPayload, "store_id", "storeId"),
        this.#storeId,
      ),
      documentId: stringValue(
        first(firstPayload, "document_id", "documentId"),
        this.#documentId,
      ),
      scope:
        rawScope === "folder" ? "folder" : ("document" as TruthViewScope),
      filter: ([
        "all",
        "facts",
        "proposed",
        "needs_review",
        "challenged",
        "unconnected",
      ] as readonly TruthClaimFilter[]).includes(rawFilter as TruthClaimFilter)
        ? (rawFilter as TruthClaimFilter)
        : query.filter,
      claims,
      counts: countsFrom(first(firstPayload, "counts"), claims),
      capabilities,
      readOnly:
        booleanValue(first(firstPayload, "read_only", "readOnly")),
      nextOffset: null,
    };
  }

  async loadClaim(claimId: string): Promise<TruthClaimDetail> {
    const payload = await this.#json(
      `${this.#base()}/claims/${encodeURIComponent(claimId)}?${this.#query()}`,
    );
    return detailFrom(payload, this.#documentId);
  }

  async proposeClaim(
    request: TruthProposeClaimRequest,
  ): Promise<TruthMutationReceipt> {
    const capture = captureBody(request);
    const claim = {
      proposition: request.proposition,
      claim_kind: request.claimKind,
    };
    const body = { ...capture, claim };
    const payload = await this.#authorizedJson({
      action: TRUTH_PROPOSE_ACTION,
      operation: "propose",
      payload: {
        ...capture,
        claim,
        claim_id: null,
      },
      url: `${this.#base()}/claims?${this.#query()}`,
      body,
    });
    this.invalidate();
    return mutationReceipt(payload);
  }

  async connectClaim(
    request: TruthConnectClaimRequest,
  ): Promise<TruthMutationReceipt> {
    const capture = captureBody(request);
    const body = { ...capture, claim_id: request.claimId };
    const payload = await this.#authorizedJson({
      action: TRUTH_CONNECT_ACTION,
      operation: "connect",
      payload: {
        ...capture,
        claim: null,
        claim_id: request.claimId,
      },
      url: `${this.#base()}/connections?${this.#query()}`,
      body,
    });
    this.invalidate();
    return mutationReceipt(payload);
  }

  async decideClaim(
    request: TruthClaimDecisionRequest,
  ): Promise<TruthMutationReceipt> {
    const body = {
      action: request.action,
      expected_canonical_sha256: request.expectedCanonicalSha256,
      expected_context_sha256: request.expectedContextSha256,
      gesture_kind: request.gestureKind,
      reason: request.reason,
    };
    const payload = await this.#authorizedJson({
      action: TRUTH_LIFECYCLE_ACTION,
      operation: "decision",
      claimId: request.claimId,
      payload: {
        claim_id: request.claimId,
        action: request.action,
        expected_canonical_sha256: request.expectedCanonicalSha256,
        expected_context_sha256: request.expectedContextSha256,
        gesture_kind: request.gestureKind ?? null,
        reason: request.reason ?? null,
      },
      url: `${this.#base()}/claims/${encodeURIComponent(request.claimId)}/decisions?${this.#query()}`,
      body,
    });
    this.invalidate();
    return mutationReceipt(payload);
  }
}

export const parseTruthClaimSummary = claimSummary;
export const parseTruthClaimDetail = detailFrom;
