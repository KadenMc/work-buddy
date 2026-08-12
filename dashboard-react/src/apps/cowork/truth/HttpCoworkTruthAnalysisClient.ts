import type {
  TruthAnalysisCapabilities,
  TruthAnalysisCandidate,
  TruthAnalysisCandidateDecisionReceipt,
  TruthAnalysisCandidateDecisionRequest,
  TruthAnalysisCandidateStatus,
  TruthAnalysisEvidenceCandidate,
  TruthAnalysisExistingClaimMatch,
  TruthAnalysisProvider,
  TruthAnalysisRun,
  TruthAnalysisRunStatus,
  TruthAnalysisSourceCoverage,
  TruthAnalysisTargetChoice,
  TruthEvidenceRelationship,
  TruthExpressionRole,
  TruthInvalidationListener,
  TruthQuoteSelector,
  TruthStartAnalysisRequest,
  TruthUnsubscribe,
} from "./contracts";
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

const objectValue = (value: unknown): JsonObject =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as JsonObject)
    : {};

const arrayValue = (value: unknown): readonly unknown[] =>
  Array.isArray(value) ? value : [];

const first = (value: JsonObject, ...keys: readonly string[]): unknown => {
  for (const key of keys) {
    if (value[key] !== undefined) return value[key];
  }
  return undefined;
};

const textValue = (value: unknown, fallback = ""): string =>
  typeof value === "string" ? value : fallback;

const nullableText = (value: unknown): string | null =>
  typeof value === "string" ? value : null;

const errorText = (value: unknown): string | null => {
  if (typeof value === "string") return value;
  const structured = objectValue(value);
  return nullableText(first(structured, "message"));
};

const booleanOrNull = (value: unknown): boolean | null =>
  typeof value === "boolean" ? value : null;

const positiveFiniteNumber = (value: unknown, message: string): number => {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value <= 0
  ) {
    throw new Error(message);
  }
  return value;
};

const nonnegativeInteger = (value: unknown, message: string): number => {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 0
  ) {
    throw new Error(message);
  }
  return value;
};

const sha256Text = (value: unknown, message: string): string => {
  if (typeof value !== "string" || !/^[a-f0-9]{64}$/u.test(value)) {
    throw new Error(message);
  }
  return value;
};

const confidenceOrNull = (value: unknown): number | null => {
  if (value === null || value === undefined) return null;
  if (typeof value === "number" && value >= 0 && value <= 1) return value;
  throw new Error("Truth analysis returned an invalid confidence value.");
};

const stringArray = (value: unknown): readonly string[] =>
  arrayValue(value).filter((item): item is string => typeof item === "string");

const RUN_STATUSES = new Set<TruthAnalysisRunStatus>([
  "queued",
  "running",
  "completed",
  "completed_with_failures",
  "failed",
  "cancelled",
]);
const CANDIDATE_STATUSES = new Set<TruthAnalysisCandidateStatus>([
  "pending",
  "saved",
  "dismissed",
]);
const RELATIONSHIPS = new Set<TruthEvidenceRelationship>([
  "supports",
  "partially_supports",
  "contradicts",
  "mentions",
  "does_not_address",
  "inconclusive",
]);
const EXPRESSION_ROLES = new Set<TruthExpressionRole>([
  "quote",
  "paraphrase",
  "summary",
  "instantiation",
]);

const runStatus = (value: unknown): TruthAnalysisRunStatus => {
  if (
    typeof value === "string" &&
    RUN_STATUSES.has(value as TruthAnalysisRunStatus)
  ) {
    return value as TruthAnalysisRunStatus;
  }
  throw new Error("Truth analysis returned an invalid run status.");
};

const candidateStatus = (value: unknown): TruthAnalysisCandidateStatus => {
  if (
    typeof value === "string" &&
    CANDIDATE_STATUSES.has(value as TruthAnalysisCandidateStatus)
  ) {
    return value as TruthAnalysisCandidateStatus;
  }
  throw new Error("Truth analysis returned an invalid candidate status.");
};

const relationship = (value: unknown): TruthEvidenceRelationship => {
  if (
    typeof value === "string" &&
    RELATIONSHIPS.has(value as TruthEvidenceRelationship)
  ) {
    return value as TruthEvidenceRelationship;
  }
  throw new Error("Truth analysis returned an invalid evidence relationship.");
};

const expressionRole = (value: unknown): TruthExpressionRole => {
  if (
    typeof value === "string" &&
    EXPRESSION_ROLES.has(value as TruthExpressionRole)
  ) {
    return value as TruthExpressionRole;
  }
  throw new Error("Truth analysis returned an invalid expression role.");
};

const quoteSelector = (raw: unknown, fallbackQuote: string): TruthQuoteSelector => {
  const value = objectValue(raw);
  const exact = textValue(first(value, "exact"));
  if (exact.length === 0 || exact !== fallbackQuote) {
    throw new Error("Truth analysis returned an invalid expression selector.");
  }
  const start = first(value, "start");
  const end = first(value, "end");
  return {
    kind: "text_quote",
    exact,
    prefix: textValue(first(value, "prefix")),
    suffix: textValue(first(value, "suffix")),
    ...(typeof start === "number" ? { start } : {}),
    ...(typeof end === "number" ? { end } : {}),
  };
};

const coverageFrom = (raw: unknown): TruthAnalysisSourceCoverage => {
  const value = objectValue(raw);
  const rawStatus = textValue(first(value, "status"));
  if (
    rawStatus !== "supplied" &&
    rawStatus !== "searched" &&
    rawStatus !== "partial" &&
    rawStatus !== "not_searched" &&
    rawStatus !== "unavailable" &&
    rawStatus !== "failed"
  ) {
    throw new Error("Truth analysis returned invalid source coverage.");
  }
  return {
    source: textValue(first(value, "source", "source_class"), "Source"),
    status: rawStatus,
    detail: nullableText(first(value, "detail")),
    externalEgress: booleanOrNull(
      first(value, "external_egress", "externalEgress"),
    ),
  };
};

export const parseTruthAnalysisCapabilities = (
  raw: unknown,
): TruthAnalysisCapabilities => {
  const value = objectValue(raw);
  if (
    first(value, "ok") !== true ||
    first(value, "schema") !== "wb.cowork.truth-analysis-capabilities/v1"
  ) {
    throw new Error("Truth analysis returned invalid execution capabilities.");
  }
  const required = objectValue(first(value, "required_cost_control"));
  const research = objectValue(first(value, "research_cost_control"));
  if (
    first(required, "enforcement_class") !== "hard_ceiling" ||
    first(required, "scope") !== "worker_model_session" ||
    first(research, "enforcement_class") !== "unavailable" ||
    first(research, "scope") !== "web_search_and_fetch" ||
    first(research, "ceiling_usd") !== null ||
    textValue(first(research, "basis")).length === 0
  ) {
    throw new Error("Truth analysis returned invalid required cost control.");
  }
  const providersRaw = first(value, "providers");
  if (!Array.isArray(providersRaw)) {
    throw new Error("Truth analysis returned invalid provider capabilities.");
  }
  const seen = new Set<string>();
  const providers = providersRaw.map((rawProvider) => {
    const provider = objectValue(rawProvider);
    const providerId = textValue(first(provider, "provider_id"));
    const analysisAvailable = first(provider, "analysis_available");
    const appliesToAllModels = first(provider, "applies_to_all_models");
    const unavailableReason = first(provider, "unavailable_reason");
    const cost = objectValue(first(provider, "cost_control"));
    const enforcementClass = first(cost, "enforcement_class");
    const rawCeiling = first(cost, "ceiling_usd_per_worker_session");
    const basis = textValue(first(cost, "basis"));
    if (
      providerId.length === 0 ||
      seen.has(providerId) ||
      typeof analysisAvailable !== "boolean" ||
      typeof appliesToAllModels !== "boolean" ||
      (unavailableReason !== null && typeof unavailableReason !== "string") ||
      (enforcementClass !== "hard_ceiling" && enforcementClass !== "unavailable") ||
      basis.length === 0
    ) {
      throw new Error("Truth analysis returned invalid provider capabilities.");
    }
    const normalizedEnforcementClass = enforcementClass as
      | "hard_ceiling"
      | "unavailable";
    const ceilingUsdPerWorkerSession =
      normalizedEnforcementClass === "hard_ceiling"
        ? positiveFiniteNumber(
            rawCeiling,
            "Truth analysis returned invalid provider cost control.",
          )
        : rawCeiling === null
          ? null
          : (() => {
              throw new Error("Truth analysis returned invalid provider cost control.");
            })();
    if (
      (analysisAvailable && unavailableReason !== null) ||
      (!analysisAvailable &&
        (typeof unavailableReason !== "string" || unavailableReason.trim().length === 0))
    ) {
      throw new Error("Truth analysis returned invalid provider availability.");
    }
    seen.add(providerId);
    return {
      providerId,
      analysisAvailable,
      unavailableReason,
      appliesToAllModels,
      costControl: {
        enforcementClass: normalizedEnforcementClass,
        ceilingUsdPerWorkerSession,
        basis,
      },
    };
  });
  return {
    schema: "wb.cowork.truth-analysis-capabilities/v1",
    requiredCostControl: {
      enforcementClass: "hard_ceiling",
      scope: "worker_model_session",
      maximumUsdPerModelSession: positiveFiniteNumber(
        first(required, "maximum_usd_per_model_session"),
        "Truth analysis returned invalid required cost control.",
      ),
    },
    researchCostControl: {
      enforcementClass: "unavailable",
      scope: "web_search_and_fetch",
      ceilingUsd: null,
      basis: textValue(first(research, "basis")),
    },
    providers,
  };
};

const coverageList = (raw: unknown): readonly TruthAnalysisSourceCoverage[] => {
  if (raw === null || raw === undefined) return [];
  if (!Array.isArray(raw)) {
    throw new Error("Truth analysis returned invalid source coverage.");
  }
  return raw.map(coverageFrom);
};

const evidenceFrom = (raw: unknown): TruthAnalysisEvidenceCandidate => {
  const value = objectValue(raw);
  const evidenceCandidateId = textValue(
    first(value, "evidence_candidate_id", "evidenceCandidateId", "id"),
  );
  if (evidenceCandidateId.length === 0) {
    throw new Error("Truth analysis returned invalid candidate evidence.");
  }
  const sourceKind = textValue(first(value, "source_kind", "sourceKind"));
  if (
    sourceKind !== "truth_span" &&
    sourceKind !== "web_fetch" &&
    sourceKind !== "passage_citation"
  ) {
    throw new Error("Truth analysis returned an invalid evidence source kind.");
  }
  const sourceLocator = textValue(
    first(value, "source_locator", "sourceLocator"),
  );
  if (sourceLocator.length === 0) {
    throw new Error("Truth analysis returned invalid candidate evidence.");
  }
  const integrity = objectValue(first(value, "integrity"));
  const integrityState =
    nullableText(first(value, "integrity_state", "integrityState")) ??
    nullableText(first(integrity, "status"));
  let capture: TruthAnalysisEvidenceCandidate["capture"] = null;
  if (sourceKind === "web_fetch") {
    const rawCapture = first(integrity, "capture");
    const captureValue = objectValue(rawCapture);
    const textTruncated = first(captureValue, "text_truncated", "textTruncated");
    const capturedTextBytes = nonnegativeInteger(
      first(captureValue, "captured_text_bytes", "capturedTextBytes"),
      "Truth analysis returned invalid web capture metadata.",
    );
    const extractedTextBytes = nonnegativeInteger(
      first(captureValue, "extracted_text_bytes", "extractedTextBytes"),
      "Truth analysis returned invalid web capture metadata.",
    );
    const maximumCapturedTextBytes = nonnegativeInteger(
      first(
        captureValue,
        "maximum_captured_text_bytes",
        "maximumCapturedTextBytes",
      ),
      "Truth analysis returned invalid web capture metadata.",
    );
    const capturedTextSha256 = sha256Text(
      first(captureValue, "captured_text_sha256", "capturedTextSha256"),
      "Truth analysis returned invalid web capture metadata.",
    );
    const fullExtractedTextSha256 = sha256Text(
      first(
        captureValue,
        "full_extracted_text_sha256",
        "fullExtractedTextSha256",
      ),
      "Truth analysis returned invalid web capture metadata.",
    );
    if (
      rawCapture === undefined ||
      typeof textTruncated !== "boolean" ||
      integrityState !== "captured_runtime" ||
      first(integrity, "content_sha256") !== capturedTextSha256 ||
      maximumCapturedTextBytes === 0 ||
      capturedTextBytes > extractedTextBytes ||
      capturedTextBytes > maximumCapturedTextBytes ||
      (textTruncated && capturedTextBytes >= extractedTextBytes) ||
      (!textTruncated && capturedTextBytes !== extractedTextBytes)
    ) {
      throw new Error("Truth analysis returned invalid web capture metadata.");
    }
    capture = {
      textTruncated,
      capturedTextBytes,
      extractedTextBytes,
      capturedTextSha256,
      fullExtractedTextSha256,
      maximumCapturedTextBytes,
    };
  }
  return {
    evidenceCandidateId,
    sourceKind,
    attachable: first(value, "attachable") === true,
    relationship: relationship(first(value, "relationship")),
    quote: nullableText(first(value, "quote")),
    sourceLocator,
    sourceTitle: nullableText(first(value, "source_title", "sourceTitle")),
    trustClass: nullableText(first(value, "trust_class", "trustClass")),
    integrityState,
    capture,
    rationale: nullableText(first(value, "rationale")),
  };
};

const existingMatchFrom = (
  raw: unknown,
): TruthAnalysisExistingClaimMatch | null => {
  if (raw === null || raw === undefined) return null;
  const value = objectValue(raw);
  const claimId = textValue(first(value, "claim_id", "claimId"));
  if (claimId.length === 0) return null;
  const rawRelationship = textValue(first(value, "relationship"));
  const proposition = textValue(first(value, "proposition"));
  if (
    rawRelationship !== "exact" &&
    rawRelationship !== "equivalent" &&
    rawRelationship !== "overlaps" &&
    rawRelationship !== "conflicts"
  ) {
    throw new Error("Truth analysis returned an invalid existing-claim match.");
  }
  if (proposition.trim().length === 0) {
    throw new Error("Truth analysis returned an invalid existing-claim match.");
  }
  return {
    claimId,
    proposition,
    relationship: rawRelationship,
    confidence: confidenceOrNull(first(value, "confidence")),
    rationale: nullableText(first(value, "rationale")),
  };
};

const candidateFrom = (raw: unknown): TruthAnalysisCandidate => {
  const value = objectValue(raw);
  const decisionValue = objectValue(first(value, "decision"));
  const rawDecision = first(decisionValue, "decision") ?? first(value, "decision_kind");
  if (
    rawDecision !== undefined &&
    rawDecision !== null &&
    rawDecision !== "save_as_proposed" &&
    rawDecision !== "connect_existing" &&
    rawDecision !== "dismiss"
  ) {
    throw new Error("Truth analysis returned an invalid candidate decision.");
  }
  const expression = objectValue(first(value, "expression"));
  const quote = textValue(first(expression, "quote"));
  const candidateId = textValue(first(value, "candidate_id", "candidateId", "id"));
  const canonicalSha256 = textValue(
      first(value, "canonical_sha256", "canonicalSha256"),
    );
  const proposition = textValue(first(value, "proposition"));
  const claimKind = textValue(first(value, "claim_kind", "claimKind"));
  if (
    candidateId.length === 0 ||
    canonicalSha256.length === 0 ||
    proposition.trim().length === 0 ||
    claimKind.trim().length === 0 ||
    quote.trim().length === 0
  ) {
    throw new Error("Truth analysis returned an invalid candidate.");
  }
  return {
    candidateId,
    canonicalSha256,
    status: candidateStatus(first(value, "status")),
    decision: typeof rawDecision === "string" ? rawDecision : null,
    proposition,
    claimKind,
    confidenceExtraction: confidenceOrNull(
      first(value, "confidence_extraction", "confidenceExtraction"),
    ),
    expression: {
      role: expressionRole(first(expression, "role")),
      quote,
      selector: quoteSelector(first(expression, "selector"), quote),
    },
    existingClaimMatch: existingMatchFrom(
      first(value, "existing_claim_match", "existingClaimMatch"),
    ),
    evidence: arrayValue(first(value, "evidence", "evidence_candidates")).map(
      evidenceFrom,
    ),
    sourceCoverage: coverageList(
      first(value, "source_coverage", "sourceCoverage"),
    ),
    limitations: stringArray(first(value, "limitations")),
  };
};

export const parseTruthAnalysisRun = (raw: unknown): TruthAnalysisRun => {
  const envelope = objectValue(raw);
  const nested = first(envelope, "analysis_run", "analysisRun", "run");
  const value =
    nested === undefined ? envelope : objectValue(nested);
  const execution = objectValue(first(value, "execution"));
  const target = objectValue(first(value, "target"));
  const rawTargetChoice = textValue(
    first(value, "target_choice", "targetChoice") ?? first(target, "choice", "source"),
  );
  if (
    rawTargetChoice !== "current_selection" &&
    rawTargetChoice !== "working_target"
  ) {
    throw new Error("Truth analysis returned an invalid target choice.");
  }
  return {
    schema: textValue(first(value, "schema"), "wb.cowork.truth-analysis/v1"),
    analysisRunId: textValue(
      first(value, "analysis_run_id", "analysisRunId", "run_id", "runId"),
    ),
    storeId: textValue(first(value, "store_id", "storeId")),
    documentId: textValue(first(value, "document_id", "documentId")),
    status: runStatus(first(value, "status")),
    targetChoice: rawTargetChoice as TruthAnalysisTargetChoice,
    targetLabel: textValue(
      first(value, "target_label", "targetLabel") ?? first(target, "label"),
      "Selected passage",
    ),
    capturedAt: textValue(
      first(value, "captured_at", "capturedAt") ?? first(target, "captured_at", "capturedAt"),
    ),
    structuredHeadSha256: textValue(
      first(value, "structured_head_sha256", "structuredHeadSha256") ??
        first(target, "structured_head_sha256", "structuredHeadSha256"),
    ),
    projectionSha256: textValue(
      first(value, "projection_sha256", "projectionSha256") ??
        first(target, "projection_sha256", "projectionSha256"),
    ),
    execution: {
      providerId: textValue(first(execution, "provider_id", "providerId")),
      modelId: textValue(first(execution, "model_id", "modelId")),
      providerLabel: textValue(
        first(execution, "provider_label", "providerLabel"),
      ),
      modelLabel: textValue(first(execution, "model_label", "modelLabel")),
    },
    candidates: arrayValue(first(value, "candidates")).map(candidateFrom),
    sourceCoverage: coverageList(
      first(value, "source_coverage", "sourceCoverage"),
    ),
    limitations: stringArray(first(value, "limitations")),
    error: errorText(first(value, "error")),
    createdAt: textValue(first(value, "created_at", "createdAt")),
    finishedAt: nullableText(first(value, "finished_at", "finishedAt")),
  };
};

const decisionReceiptFrom = (
  raw: unknown,
): TruthAnalysisCandidateDecisionReceipt => {
  const value = objectValue(raw);
  const ok = first(value, "ok");
  const analysisRunId = textValue(
    first(value, "analysis_run_id", "analysisRunId", "run_id", "runId"),
  );
  const candidateId = textValue(first(value, "candidate_id", "candidateId"));
  if (ok !== true || analysisRunId.length === 0 || candidateId.length === 0) {
    throw new Error("Truth analysis returned an invalid decision receipt.");
  }
  return {
    ok,
    analysisRunId,
    candidateId,
    candidateStatus: candidateStatus(
      first(value, "candidate_status", "candidateStatus", "status"),
    ),
    claimId: nullableText(first(value, "claim_id", "claimId")),
    expressionId: nullableText(
      first(value, "expression_id", "expressionId"),
    ),
  };
};

const errorMessage = async (response: Response, fallback: string): Promise<string> => {
  try {
    const payload = objectValue(await response.json());
    const nested = objectValue(first(payload, "error"));
    const message = first(nested, "message") ?? first(payload, "message", "error");
    if (typeof message === "string" && message.trim().length > 0) return message;
  } catch {
    // Keep the stable fallback for malformed server/proxy output.
  }
  return fallback;
};

export interface HttpCoworkTruthAnalysisClientOptions {
  readonly storeId: string;
  readonly documentId: string;
  readonly fetchImpl?: typeof fetch;
}

/** Same-origin transport for durable AI-prepared Truth candidates. */
export class HttpCoworkTruthAnalysisClient implements TruthAnalysisProvider {
  readonly #storeId: string;
  readonly #documentId: string;
  readonly #fetch: typeof fetch;
  readonly #listeners = new Set<TruthInvalidationListener>();

  constructor(options: HttpCoworkTruthAnalysisClientOptions) {
    this.#storeId = options.storeId;
    this.#documentId = options.documentId;
    this.#fetch = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  #base(): string {
    return `/api/truth/doc/${encodeURIComponent(this.#documentId)}/truth/analysis-runs`;
  }

  #capabilitiesBase(): string {
    return `/api/truth/doc/${encodeURIComponent(this.#documentId)}/truth/analysis-capabilities`;
  }

  #query(): string {
    return `store_id=${encodeURIComponent(this.#storeId)}`;
  }

  subscribe(listener: TruthInvalidationListener): TruthUnsubscribe {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  invalidate(): void {
    for (const listener of this.#listeners) listener();
  }

  async #get(url: string, allowMissing = false): Promise<unknown | null> {
    const response = await this.#fetch(url, { credentials: "same-origin" });
    if (allowMissing && response.status === 404) return null;
    if (!response.ok) {
      throw new Error(
        await errorMessage(response, "Truth analysis could not be loaded."),
      );
    }
    return response.json();
  }

  async loadCapabilities(): Promise<TruthAnalysisCapabilities> {
    const payload = await this.#get(
      `${this.#capabilitiesBase()}?${this.#query()}`,
    );
    return parseTruthAnalysisCapabilities(payload);
  }

  async loadCurrent(): Promise<TruthAnalysisRun | null> {
    const payload = await this.#get(
      `${this.#base()}/current?${this.#query()}`,
      true,
    );
    if (payload === null) return null;
    const value = objectValue(payload);
    if (
      first(value, "analysis_run", "analysisRun", "run") === null ||
      first(value, "current") === null
    ) {
      return null;
    }
    const run = parseTruthAnalysisRun(payload);
    return run.analysisRunId.length === 0 ? null : run;
  }

  async loadRun(analysisRunId: string): Promise<TruthAnalysisRun> {
    const payload = await this.#get(
      `${this.#base()}/${encodeURIComponent(analysisRunId)}?${this.#query()}`,
    );
    const run = parseTruthAnalysisRun(payload);
    if (run.analysisRunId.length === 0) {
      throw new Error("Truth analysis returned an invalid run.");
    }
    return run;
  }

  async start(request: TruthStartAnalysisRequest): Promise<TruthAnalysisRun> {
    const body: JsonObject = {
      capture: request.capture,
      execution: {
        provider_id: request.execution.providerId,
        model_id: request.execution.modelId,
      },
    };
    const identity = await initializeLocalIdentity({ fetchImpl: this.#fetch });
    if (!identity.authenticated) {
      throw new Error(
        identity.reason || "An authenticated local session is required.",
      );
    }
    const subject = `cowork-truth-analysis-start:${this.#documentId}`;
    const contextSha256 = await sha256Hex(
      canonicalJson({
        schema: "wb.cowork.truth-analysis-start-gesture/v1",
        store_id: this.#storeId,
        document_id: this.#documentId,
        capture: request.capture,
        execution: body.execution,
      }),
    );
    const gesture = await issueHumanGesture(
      {
        action: "cowork.truth.analysis_start",
        subject,
        contextSha256,
      },
      this.#fetch,
    );
    const response = await this.#fetch(`${this.#base()}?${this.#query()}`, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        ...localIdentityHeaders(gesture.token),
      },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      throw new Error(
        await errorMessage(
          response,
          "Truth could not analyze this exact passage.",
        ),
      );
    }
    const run = parseTruthAnalysisRun(await response.json());
    if (run.analysisRunId.length === 0) {
      throw new Error("Truth analysis returned an invalid start receipt.");
    }
    this.invalidate();
    return run;
  }

  async decideCandidate(
    request: TruthAnalysisCandidateDecisionRequest,
  ): Promise<TruthAnalysisCandidateDecisionReceipt> {
    const body: JsonObject = {
      decision: request.decision,
      expected_canonical_sha256: request.expectedCanonicalSha256,
      ...(request.decision === "connect_existing"
        ? { existing_claim_id: request.existingClaimId }
        : {}),
      ...((request.decision === "save_as_proposed" ||
        request.decision === "connect_existing") &&
      request.edits !== undefined
        ? {
            edits: {
              proposition: request.edits.proposition,
              claim_kind: request.edits.claimKind,
              expression_role: request.edits.expressionRole,
              evidence_candidate_ids: request.edits.evidenceCandidateIds,
            },
          }
        : {}),
    };
    const identity = await initializeLocalIdentity({ fetchImpl: this.#fetch });
    if (!identity.authenticated) {
      throw new Error(
        identity.reason || "An authenticated local session is required.",
      );
    }
    const subject = `cowork-truth-candidate-decision:${request.analysisRunId}:${request.candidateId}`;
    const contextSha256 = await sha256Hex(
      canonicalJson({
        schema: "wb.cowork.truth-candidate-decision-gesture/v1",
        store_id: this.#storeId,
        document_id: this.#documentId,
        analysis_run_id: request.analysisRunId,
        candidate_id: request.candidateId,
        expected_canonical_sha256: request.expectedCanonicalSha256,
        decision: request.decision,
        existing_claim_id:
          request.decision === "connect_existing"
            ? request.existingClaimId
            : null,
        edits: body.edits ?? null,
      }),
    );
    const gesture = await issueHumanGesture(
      {
        action: "cowork.truth.candidate_decision",
        subject,
        contextSha256,
      },
      this.#fetch,
    );
    const response = await this.#fetch(
      `${this.#base()}/${encodeURIComponent(request.analysisRunId)}/candidates/${encodeURIComponent(request.candidateId)}/decisions?${this.#query()}`,
      {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          ...localIdentityHeaders(gesture.token),
        },
        body: JSON.stringify(body),
      },
    );
    if (!response.ok) {
      throw new Error(
        await errorMessage(response, "Truth could not save that decision."),
      );
    }
    const receipt = decisionReceiptFrom(await response.json());
    this.invalidate();
    return receipt;
  }
}
