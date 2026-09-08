import type { ReviewExpression } from "../rail/contracts";

import type {
  TruthClaimDecisionRequest, TruthClaimDetail, TruthClaimsSnapshot,
  TruthConnectClaimRequest, TruthEvidenceReceipt, TruthMutationReceipt,
  TruthPassageConnection, TruthProposeClaimRequest, TruthQuery,
  TruthRailProvider, TruthSelectionCapture,
} from "./contracts";
import { truthClaimMatchesFilter } from "./contracts";

const CREATED_AT = new Date(0).toISOString();
const id = (value: number): string => value.toString(16).padStart(32, "0");
const fingerprint = (value: string): string => {
  let hash = 0;
  for (const character of value) hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  return hash.toString(16).padStart(8, "0").repeat(8);
};

const receipt = (number: number, quote: string, sourceLocator: string): TruthEvidenceReceipt => ({
  linkId: `demo-link-${number}`, spanId: `demo-evidence-span-${number}`,
  evidenceId: `ev-${number}`, evidenceKind: "measurement", quote, sourceLocator,
  trustClass: "measurement", authorKind: "human", authorRef: "demo-author",
  active: true, spanSha256: fingerprint(quote), contentSha256: fingerprint(sourceLocator),
  mediaType: "application/json", derivedFromStore: null, acquiredAt: CREATED_AT,
  acquisitionMethod: "fixture", spanRedactedAt: null, evidenceRedactedAt: null,
  integrity: { state: "valid", detail: null, locatorScheme: "file", verifiabilityClass: "snapshot", snapshotPresent: true },
});

const bind = (claim: TruthClaimDetail): TruthClaimDetail => ({
  ...claim,
  decisionBinding: {
    payloadSha256: claim.canonicalSha256,
    contextSha256: fingerprint(JSON.stringify({
      status: claim.baseStatus, needsReview: claim.needsReview, redacted: claim.redacted,
      connections: claim.connections, receipts: claim.receipts, lifecycle: claim.lifecycle,
    })),
    agentAuthoredOnly: claim.support.agentAuthoredOnly,
  },
});

const createClaim = (
  claimId: string, proposition: string, claimKind: string,
  connections: readonly TruthPassageConnection[], receipts: readonly TruthEvidenceReceipt[] = [],
): TruthClaimDetail => bind({
  claimId, proposition, claimKind, canonicalSha256: fingerprint(`${claimKind}:${proposition}`),
  scope: "store", baseStatus: "proposed", needsReview: false, health: "clean",
  healthReason: null, voided: false, redacted: false, validFrom: null, validTo: null,
  effectiveValidFrom: null, effectiveValidTo: null, evidenceCount: receipts.length,
  connectionCount: connections.length, connections, createdAt: CREATED_AT,
  createdBy: { kind: "human", ref: "demo-author" }, isFact: false,
  availableActions: ["confirm", "reject", "redact"], structured: {}, receipts,
  lifecycle: [{ eventId: `${claimId}:proposed`, status: "proposed", at: CREATED_AT, actorKind: "human", actorRef: "demo-author", note: null }],
  conflicts: [], derivations: [], support: {
    supportSpanIds: receipts.map((item) => item.spanId), usableSpanIds: receipts.map((item) => item.spanId),
    quarantinedOnly: false, agentAuthoredOnly: false, storeDerivedOnly: false,
  }, premises: { localUnconfirmed: [], unresolvedUris: [], confirmed: true },
  decisionBinding: null,
});

export interface InMemoryTruthSeed {
  readonly storeId?: string;
  readonly documentId?: string;
  readonly claims?: readonly TruthClaimDetail[];
}

/** Mutable, transport-free Truth fixture. It never writes the user's ledger. */
export class InMemoryTruthProvider implements TruthRailProvider {
  readonly storeId: string;
  readonly documentId: string;
  #claims: readonly TruthClaimDetail[];
  #sequence = 100;
  readonly #listeners = new Set<() => void>();

  constructor(seed: InMemoryTruthSeed = {}) {
    this.storeId = seed.storeId ?? "demo";
    this.documentId = seed.documentId ?? "demo-doc";
    this.#claims = seed.claims ?? this.#demoClaims();
  }

  #demoClaims(): readonly TruthClaimDetail[] {
    const connection = (expressionId: string, quote: string, start: number): TruthPassageConnection => ({
      expressionId, spanId: `sp-${expressionId}`, documentId: this.documentId,
      documentTitle: "Context bundle cache", documentPath: "context-bundle-cache.md",
      role: "quote", quote, selector: { kind: "text_quote", exact: quote, prefix: "", suffix: "", start, end: start + quote.length },
      currentDocument: true, stale: null, claimCanonicalSha256: "", createdAt: CREATED_AT,
      createdBy: { kind: "human", ref: "demo-author" },
    });
    const connect = (claim: TruthClaimDetail): TruthClaimDetail => bind({ ...claim,
      connections: claim.connections.map((item) => ({ ...item, claimCanonicalSha256: claim.canonicalSha256 })),
    });
    const benchmark = createClaim(id(1), "Cold-start latency dropped from 1.8 s to 1.1 s after prewarming.", "measurement", [
      connection("x1", "cold-start latency dropped from 1.8 s to 1.1 s after prewarming", 250),
    ], [
      receipt(1, "prewarm run A: cold-start 1.12 s", "benchmarks/prewarm-a.json"),
      receipt(2, "prewarm run B: cold-start 1.08 s", "benchmarks/prewarm-b.json"),
    ]);
    const collector = createClaim(id(4), "A collector records its identity, configuration revision, and contributed content fingerprint.", "fact", [
      connection("x5", "A collector records its identity, configuration revision, and the content fingerprint it contributed to the bundle.", 460),
    ]);
    return [
      connect(bind({ ...benchmark, baseStatus: "confirmed", isFact: true, availableActions: ["reaffirm", "redact"],
        lifecycle: [...benchmark.lifecycle, { eventId: "demo-confirmed", status: "confirmed", at: CREATED_AT, actorKind: "human", actorRef: "demo-author", note: "Reviewed both benchmark runs." }],
      })),
      connect(createClaim(id(2), "A cached bundle can be reused while its collector inputs are unchanged.", "fact", [
        connection("x2", "a bundle is reused across invocations that share it", 60),
        connection("x3", "The cache reuses a bundle only while those inputs still describe the requested context.", 580),
      ])),
      connect(createClaim(id(3), "Every reported change rebuilds the bundle.", "fact", [
        connection("x4", "We always rebuild the bundle when a reported change lands.", 175),
      ])),
      connect(bind({ ...collector, baseStatus: "confirmed", needsReview: true, health: "needs_review", healthReason: "active_needs_review_overlay", isFact: false,
        availableActions: ["reaffirm", "redact"],
        lifecycle: [...collector.lifecycle,
          { eventId: "demo-collector-confirmed", status: "confirmed", at: CREATED_AT, actorKind: "human", actorRef: "demo-author", note: null },
          { eventId: "demo-collector-review", status: "needs_review", at: CREATED_AT, actorKind: "human", actorRef: "demo-author", note: "Recheck the collector's recorded inputs." },
        ],
      })),
    ];
  }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  #replace(claim: TruthClaimDetail): void {
    const existing = this.#claims.some((item) => item.claimId === claim.claimId);
    this.#claims = existing ? this.#claims.map((item) => item.claimId === claim.claimId ? claim : item) : [...this.#claims, claim];
    for (const listener of this.#listeners) listener();
  }

  /** Reproject this after each invalidation to keep editor marks in sync. */
  getExpressions(): readonly ReviewExpression[] {
    return this.#claims.flatMap((claim) => claim.connections.filter((item) => item.documentId === this.documentId).map((connection) => ({
      expressionId: connection.expressionId, spanId: connection.spanId, nodeIdHint: null,
      quote: connection.quote, quoteAnchor: connection.selector,
      claimRef: `wb-truth://${this.storeId}/claim/${claim.claimId}`,
      claimStatus: claim.needsReview ? "needs_review" as const : claim.baseStatus === "unknown" ? null : claim.baseStatus,
      claimKind: claim.claimKind, isFact: claim.isFact, proposition: claim.redacted ? "" : claim.proposition,
      evidenceCount: claim.evidenceCount, stale: connection.stale ?? null,
    })));
  }

  async load(query: TruthQuery): Promise<TruthClaimsSnapshot> {
    const claims = this.#claims.filter((claim) => query.scope === "folder" || claim.connections.some((item) => item.documentId === this.documentId));
    return {
      schema: "wb.cowork.truth/v1", storeId: this.storeId, documentId: this.documentId, ...query,
      claims: claims.filter((claim) => truthClaimMatchesFilter(claim, query.filter)),
      counts: { all: claims.length, facts: claims.filter((item) => item.isFact).length,
        proposed: claims.filter((item) => item.baseStatus === "proposed").length,
        needsReview: claims.filter((item) => item.needsReview).length,
        challenged: claims.filter((item) => item.baseStatus === "challenged").length,
        unconnected: claims.filter((item) => item.connectionCount === 0).length },
      capabilities: { canObserve: true, canModify: true, canDecide: true,
        allowedClaimKinds: ["fact", "measurement", "decision"], mutationUnavailableReason: null },
      readOnly: false, nextOffset: null,
    };
  }

  async loadClaim(claimId: string): Promise<TruthClaimDetail> {
    const claim = this.#claims.find((item) => item.claimId === claimId);
    if (claim === undefined) throw new Error("This demo claim is unavailable.");
    return claim;
  }

  #validateCapture(capture: TruthSelectionCapture): void {
    if (capture.storeId !== this.storeId || capture.documentId !== this.documentId || capture.selector.exact.trim().length === 0) {
      throw new Error("Select a passage in this demo document before adding a claim.");
    }
  }

  async proposeClaim(request: TruthProposeClaimRequest): Promise<TruthMutationReceipt> {
    this.#validateCapture(request.capture);
    const proposition = request.proposition.trim();
    if (proposition.length === 0) throw new Error("Write the claim before adding it.");
    const matching = this.#claims.find((claim) => claim.proposition === proposition && claim.claimKind === request.claimKind);
    if (matching !== undefined) return this.connectClaim({ ...request, claimId: matching.claimId });
    const claim = createClaim(id(++this.#sequence), proposition, request.claimKind, []);
    const connection = this.#connection(claim, request);
    this.#replace(bind({ ...claim, connections: [connection], connectionCount: 1 }));
    return { ok: true, claimId: claim.claimId, claimCreated: true, expressionId: connection.expressionId, expressionCreated: true, status: "proposed" };
  }

  #connection(claim: TruthClaimDetail, request: TruthConnectClaimRequest | TruthProposeClaimRequest): TruthPassageConnection {
    const expressionId = `demo-expression-${++this.#sequence}`;
    return { expressionId, spanId: `span-${expressionId}`, documentId: this.documentId,
      documentTitle: "Context bundle cache", documentPath: "context-bundle-cache.md", role: request.role,
      quote: request.capture.selector.exact, selector: request.capture.selector, currentDocument: true,
      stale: null, claimCanonicalSha256: claim.canonicalSha256, createdAt: new Date().toISOString(),
      createdBy: { kind: "human", ref: "demo-author" } };
  }

  async connectClaim(request: TruthConnectClaimRequest): Promise<TruthMutationReceipt> {
    this.#validateCapture(request.capture);
    const claim = await this.loadClaim(request.claimId);
    if (claim.redacted || claim.voided || ["rejected", "retracted", "superseded", "expired"].includes(claim.baseStatus)) {
      throw new Error("This claim is no longer active. Revise the wording to propose a corrected claim.");
    }
    const existing = claim.connections.find((item) => item.documentId === request.capture.documentId && item.role === request.role && JSON.stringify(item.selector) === JSON.stringify(request.capture.selector));
    const connection = existing ?? this.#connection(claim, request);
    if (existing === undefined) this.#replace(bind({ ...claim, connections: [...claim.connections, connection], connectionCount: claim.connectionCount + 1 }));
    return { ok: true, claimId: claim.claimId, claimCreated: false, expressionId: connection.expressionId, expressionCreated: existing === undefined, status: claim.baseStatus };
  }

  async decideClaim(request: TruthClaimDecisionRequest): Promise<TruthMutationReceipt> {
    const claim = await this.loadClaim(request.claimId);
    if (request.expectedCanonicalSha256 !== claim.decisionBinding?.payloadSha256 || request.expectedContextSha256 !== claim.decisionBinding.contextSha256) {
      throw new Error("This claim changed. Review it again before deciding.");
    }
    if (!claim.availableActions.includes(request.action)) throw new Error("This decision is unavailable for the claim's current state.");
    if ((request.action === "confirm" || request.action === "reaffirm") && request.gestureKind !== request.action) throw new Error("Review and confirm this exact claim first.");
    if (request.action === "redact" && request.reason === undefined) throw new Error("Choose a reason for redaction.");
    const rejected = request.action === "reject";
    const redacted = request.action === "redact";
    const baseStatus = rejected ? "rejected" : redacted ? claim.baseStatus : "confirmed";
    this.#replace(bind({ ...claim, baseStatus, needsReview: false,
      health: redacted ? "redacted" : "clean", redacted, isFact: !redacted && !rejected,
      proposition: redacted ? "Redacted claim" : claim.proposition,
      availableActions: redacted ? [] : rejected ? ["redact"] : ["reaffirm", "redact"],
      connections: claim.connections.map((item) => ({ ...item, stale: rejected || redacted ? "claim_terminal" : item.stale ?? null })),
      lifecycle: [...claim.lifecycle, { eventId: `demo-event-${++this.#sequence}`, status: request.action === "reaffirm" ? "reaffirmed" : redacted ? "redacted" : baseStatus, at: new Date().toISOString(), actorKind: "human", actorRef: "demo-author", note: request.reason ?? null }],
    }));
    return { ok: true, claimId: claim.claimId, claimCreated: false, expressionId: null, expressionCreated: false, status: baseStatus };
  }
}
