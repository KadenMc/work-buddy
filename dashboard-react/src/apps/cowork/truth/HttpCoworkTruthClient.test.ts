import { beforeEach, describe, expect, it, vi } from "vitest";

import { resetLocalIdentityForTests } from "../../../security/localIdentity";
import type { TruthSelectionCapture } from "./contracts";
import { HttpCoworkTruthClient } from "./HttpCoworkTruthClient";

const jsonResponse = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const capture: TruthSelectionCapture = {
  schema: "wb.cowork.truth-selection/v1",
  captureId: "capture-1",
  storeId: "store-1",
  documentId: "doc-1",
  structuredHeadSha256: "a".repeat(64),
  ydocGenerationSha256: "b".repeat(64),
  projectionSha256: "c".repeat(64),
  label: "Selection",
  wordCount: 3,
  selector: {
    kind: "text_quote",
    exact: "Selected source text",
    prefix: "Before ",
    suffix: " after",
    start: 10,
    end: 30,
  },
};

const authenticatedFetch = (
  response: (input: RequestInfo | URL, init?: RequestInit) => Response | Promise<Response>,
) => {
  let gestureIndex = 0;
  return vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input);
    if (url === "/api/local-identity/session/csrf") {
      return jsonResponse({
        ok: true,
        authenticated: true,
        csrf_token: "csrf-token",
        principal: {
          actor: {
            schema: "wb.actor-ref/v1",
            issuer_authority_id: "issuer-authority",
            subject: "dashboard-user",
            kind: "human",
            tenant_scope_id: "tenant-scope",
          },
          origin: window.location.origin,
          audience: "work-buddy-dashboard",
          session_expires_at: 9_999_999_999,
          rotation_due_at: 9_999_999_000,
          assurance: "enrolled_local_session",
        },
      });
    }
    if (url === "/api/local-identity/gestures") {
      gestureIndex += 1;
      const request = JSON.parse(String(init?.body)) as {
        action: string;
        subject: string;
        context_sha256: string;
      };
      return jsonResponse({
        ok: true,
        gesture: {
          token: `gesture-${gestureIndex}`,
          action: request.action,
          subject_sha256: "a".repeat(64),
          context_sha256: request.context_sha256,
          expires_at: 9_999_999_999,
        },
      });
    }
    return response(input, init);
  });
};

describe("HttpCoworkTruthClient", () => {
  beforeEach(() => resetLocalIdentityForTests());

  it("loads and explicitly transitions a provenance-first Truth policy", async () => {
    const capabilityEnvelope = (
      activation: "disabled" | "enabled",
      revision: number,
    ) => ({
      schema: "wb.cowork-document-capabilities/v1",
      interaction_contract: {
        contract_id: "work-buddy.working-document",
        version: 1,
        digest: "a".repeat(64),
      },
      modules: {
        review: true,
        provenance: true,
        chat: true,
        truth: activation === "enabled",
      },
      truth: {
        eligibility: "allowed",
        activation,
        activation_revision: revision,
        policy_fingerprint: `policy-${revision}`,
        ledger_present: false,
        unavailable_reason: null,
      },
    });
    const fetchImpl = authenticatedFetch(async (input, init) => {
      const url = String(input);
      if (url.includes("/policy?")) {
        return jsonResponse({
          ok: true,
          capability_envelope: capabilityEnvelope("disabled", 1),
          document_head_sha256: "b".repeat(64),
        });
      }
      expect(url).toContain("/activation?store_id=store-1");
      expect(init?.method).toBe("POST");
      expect(JSON.parse(String(init?.body))).toEqual({
        next_state: "enabled",
        expected_activation_revision: 1,
        expected_interaction_contract_sha256: "a".repeat(64),
        expected_document_head_sha256: "b".repeat(64),
        intent_id: "intent-enable",
        reason: "Enable for this working document.",
      });
      return jsonResponse({
        ok: true,
        capability_envelope: capabilityEnvelope("enabled", 2),
        document_head_sha256: "b".repeat(64),
      });
    });
    const client = new HttpCoworkTruthClient({
      storeId: "store-1",
      documentId: "doc-1",
      fetchImpl,
    });

    const observed = await client.loadActivationPolicy();
    expect(observed.capabilityEnvelope.truth.activation).toBe("disabled");
    const changed = await client.transitionTruthActivation({
      nextState: "enabled",
      expectedActivationRevision: 1,
      expectedInteractionContractSha256: "a".repeat(64),
      expectedDocumentHeadSha256: "b".repeat(64),
      intentId: "intent-enable",
      reason: "Enable for this working document.",
    });

    expect(changed.capabilityEnvelope.truth.activation).toBe("enabled");
    const gestureRequest = fetchImpl.mock.calls.find(
      ([input]) => String(input) === "/api/local-identity/gestures",
    );
    expect(JSON.parse(String(gestureRequest?.[1]?.body))).toMatchObject({
      action: "cowork.truth.activation.change",
      subject: "cowork-truth:activation:store-1:doc-1",
    });
  });

  it("loads every page and never overrides an authoritative non-fact classification", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async (input) => {
      const offset = new URL(String(input), "https://work-buddy.test").searchParams.get("offset");
      const secondPage = offset === "1";
      return jsonResponse({
        schema: "cowork-truth/v1",
        store_id: "store-1",
        document_id: "doc-1",
        view: "folder",
        filter: "all",
        counts: { all: 2, facts: 0, proposed: 0, needs_review: 0, challenged: 0, unconnected: 2 },
        next_offset: secondPage ? null : 1,
        capabilities: {
          can_observe: true,
          can_modify: true,
          can_decide: true,
          allowed_claim_kinds: ["fact"],
          mutation_unavailable_reason: null,
        },
        claims: [{
          claim_id: secondPage ? "claim-2" : "claim-1",
          proposition: secondPage ? "Second claim" : "Expired-by-time claim",
          claim_kind: "fact",
          canonical_sha256: secondPage ? "b".repeat(64) : "a".repeat(64),
          scope: "store",
          base_status: "confirmed",
          needs_review: false,
          health: "clean",
          voided: false,
          redacted: false,
          is_fact: false,
          receipt_count: 0,
          connection_count: 0,
          document_connections: [],
          available_actions: [],
          created_at: "2026-08-04T12:00:00Z",
        }],
      });
    });
    const client = new HttpCoworkTruthClient({
      storeId: "store-1",
      documentId: "doc-1",
      fetchImpl,
    });

    const result = await client.load({ scope: "folder", filter: "all" });

    expect(result.claims.map((claim) => claim.claimId)).toEqual([
      "claim-1",
      "claim-2",
    ]);
    expect(result.claims.every((claim) => claim.isFact === false)).toBe(true);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(String(fetchImpl.mock.calls[0][0])).toContain("limit=200");
    expect(String(fetchImpl.mock.calls[1][0])).toContain("offset=1");
    expect(result.nextOffset).toBeNull();
  });

  it("parses the authoritative list/detail wire shape without requiring selector kind", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async (input) => {
      if (String(input).includes("/claims/claim-1")) {
        return jsonResponse({
          claim: {
            claim_id: "claim-1",
            proposition: "The document has a heading.",
            claim_kind: "fact",
            canonical_sha256: "canonical",
            scope: "store",
            base_status: "proposed",
            needs_review: false,
            health: "clean",
            voided: false,
            redacted: false,
            receipt_count: 1,
            connection_count: 1,
            document_connections: [],
            available_actions: ["confirm", "reject", "redact"],
            created_at: "2026-08-04T12:00:00Z",
            created_by: { kind: "human", ref: "owner" },
            provenance: {
              prepared_by: {
                kind: "agent_run",
                surface: "cowork_truth_analysis",
                analysis_run_id: "run-1",
                candidate_id: "candidate-1",
                provider_id: "claude-code",
                model_id: "sonnet",
              },
              added_by: {
                kind: "human",
                ref: "owner",
                at: "2026-08-04T12:00:05Z",
              },
            },
          },
          connections: [{
            expression_id: "expression-1",
            span_id: "span-1",
            document_id: "doc-1",
            document_title: "Draft",
            role: "quote",
            quote: "Selected source text",
            selector: { exact: "Selected source text", prefix: "", suffix: "", start: 10, end: 30 },
            provenance: {
              prepared_by: {
                kind: "agent_run",
                surface: "cowork_truth_analysis",
                analysis_run_id: "run-1",
                candidate_id: "candidate-1",
                provider_id: "claude-code",
                model_id: "sonnet",
              },
              added_by: {
                kind: "human",
                ref: "owner",
                at: "2026-08-04T12:00:05Z",
              },
            },
          }],
          status_history: [{ id: "event-1", status: "proposed", at: "2026-08-04T12:00:00Z", actor_kind: "agent" }],
          receipts: [{
            link_id: "link-1",
            span_id: "evidence-span-1",
            evidence_id: "evidence-1",
            evidence_kind: "web",
            source_locator: "https://example.test/source",
            trust_class: "primary",
            author: { kind: "human", ref: "owner" },
          }],
          conflicts: [],
          derivations: [],
          decision_binding: {
            payload_sha256: "payload-hash",
            context_sha256: "context-hash",
            agent_authored_only: true,
          },
        });
      }
      return jsonResponse({
        schema: "cowork-truth/v1",
        store_id: "store-1",
        document_id: "doc-1",
        view: "document",
        filter: "all",
        counts: { all: 1, facts: 0, proposed: 1, needs_review: 0, challenged: 0, unconnected: 0 },
        next_offset: null,
        capabilities: { can_observe: true, can_modify: true, can_decide: true, allowed_claim_kinds: ["fact", "decision"] },
        claims: [{
          claim_id: "claim-1",
          proposition: "The document has a heading.",
          claim_kind: "fact",
          canonical_sha256: "canonical",
          scope: "store",
          base_status: "proposed",
          needs_review: false,
          health: "clean",
          voided: false,
          redacted: false,
          receipt_count: 1,
          connection_count: 1,
          document_connections: [{
            expression_id: "expression-1",
            span_id: "span-1",
            document_id: "doc-1",
            document_title: "Draft",
            role: "quote",
            quote: "Selected source text",
            selector: { exact: "Selected source text", prefix: "", suffix: "", start: 10, end: 30 },
          }],
          available_actions: ["confirm", "reject", "redact"],
          created_at: "2026-08-04T12:00:00Z",
        }],
      });
    });
    const client = new HttpCoworkTruthClient({ storeId: "store-1", documentId: "doc-1", fetchImpl });

    const snapshot = await client.load({ scope: "document", filter: "all" });
    expect(snapshot.capabilities.allowedClaimKinds).toEqual(["fact", "decision"]);
    expect(snapshot.claims[0]).toMatchObject({
      evidenceCount: 1,
      connectionCount: 1,
      availableActions: ["confirm", "reject", "redact"],
    });
    expect(snapshot.claims[0].connections[0].selector).toMatchObject({
      kind: "text_quote",
      exact: "Selected source text",
    });

    const detail = await client.loadClaim("claim-1");
    expect(detail.connections).toHaveLength(1);
    expect(detail.lifecycle[0]).toMatchObject({ eventId: "event-1", actorKind: "agent" });
    expect(detail.receipts[0]).toMatchObject({ authorKind: "human", authorRef: "owner" });
    expect(detail.decisionBinding).toEqual({
      payloadSha256: "payload-hash",
      contextSha256: "context-hash",
      agentAuthoredOnly: true,
    });
    expect(detail.provenance).toMatchObject({
      preparedBy: {
        analysisRunId: "run-1",
        candidateId: "candidate-1",
        providerId: "claude-code",
        modelId: "sonnet",
      },
      addedBy: { kind: "human", ref: "owner" },
    });
    expect(detail.connections[0].provenance?.preparedBy).toMatchObject({
      providerId: "claude-code",
      modelId: "sonnet",
    });
  });

  it("posts selection-bound create/connect requests and exact guarded decisions", async () => {
    const fetchImpl = authenticatedFetch(async () =>
      jsonResponse({
        ok: true,
        claim_id: "claim-1",
        claim_created: true,
        expression_id: "expression-1",
        expression_created: true,
      }, 201),
    );
    const client = new HttpCoworkTruthClient({ storeId: "store-1", documentId: "doc-1", fetchImpl });

    await client.proposeClaim({ capture, proposition: "A precise claim.", claimKind: "fact", role: "quote" });
    await client.connectClaim({ capture, claimId: "claim-2", role: "paraphrase" });
    await client.decideClaim({
      claimId: "claim-1",
      action: "confirm",
      expectedCanonicalSha256: "payload-hash",
      expectedContextSha256: "context-hash",
      gestureKind: "confirm",
    });

    const mutations = fetchImpl.mock.calls.filter(([input]) =>
      String(input).startsWith("/api/truth/"),
    );
    const propose = JSON.parse(String(mutations[0][1]?.body)) as Record<string, unknown>;
    expect(propose).toMatchObject({
      expected_structured_head_sha256: capture.structuredHeadSha256,
      expected_ydoc_generation_sha256: capture.ydocGenerationSha256,
      expected_projection_sha256: capture.projectionSha256,
      selector: capture.selector,
      role: "quote",
      claim: { proposition: "A precise claim.", claim_kind: "fact" },
    });
    const connect = JSON.parse(String(mutations[1][1]?.body)) as Record<string, unknown>;
    expect(connect).toMatchObject({ claim_id: "claim-2", role: "paraphrase" });
    const decision = JSON.parse(String(mutations[2][1]?.body)) as Record<string, unknown>;
    expect(decision).toMatchObject({
      action: "confirm",
      expected_canonical_sha256: "payload-hash",
      expected_context_sha256: "context-hash",
      gesture_kind: "confirm",
    });
    expect(mutations.map(([, init]) => init?.headers)).toEqual([
      expect.objectContaining({ "X-WB-CSRF": "csrf-token", "X-WB-Gesture": "gesture-1" }),
      expect.objectContaining({ "X-WB-CSRF": "csrf-token", "X-WB-Gesture": "gesture-2" }),
      expect.objectContaining({ "X-WB-CSRF": "csrf-token", "X-WB-Gesture": "gesture-3" }),
    ]);
    const gestures = fetchImpl.mock.calls
      .filter(([input]) => String(input) === "/api/local-identity/gestures")
      .map(([, init]) => JSON.parse(String(init?.body)) as Record<string, unknown>);
    expect(gestures.map((item) => item.action)).toEqual([
      "cowork.truth.propose_and_connect",
      "cowork.truth.connect",
      "cowork.truth.claim_decision",
    ]);
  });

  it("preserves idempotency flags from connection receipts", async () => {
    const client = new HttpCoworkTruthClient({
      storeId: "store-1",
      documentId: "doc-1",
      fetchImpl: authenticatedFetch(async () =>
        jsonResponse({
          ok: true,
          claim_id: "claim-1",
          claim_created: false,
          expression_id: "expression-1",
          expression_created: false,
        }),
      ),
    });

    await expect(
      client.connectClaim({ capture, claimId: "claim-1", role: "quote" }),
    ).resolves.toMatchObject({
      claimCreated: false,
      expressionCreated: false,
    });
  });

  it("fails closed before a Truth mutation when local identity is unavailable", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      jsonResponse({
        ok: false,
        authenticated: false,
        error: { code: "local_session_required", message: "No local session." },
      }, 401),
    );
    const client = new HttpCoworkTruthClient({
      storeId: "store-1",
      documentId: "doc-1",
      fetchImpl,
    });

    await expect(
      client.connectClaim({ capture, claimId: "claim-1", role: "quote" }),
    ).rejects.toThrow("No authenticated local session");
    expect(fetchImpl).toHaveBeenCalledOnce();
    expect(String(fetchImpl.mock.calls[0][0])).toBe(
      "/api/local-identity/session/csrf",
    );
  });

  it("surfaces the server's nested actionable error message", async () => {
    const client = new HttpCoworkTruthClient({
      storeId: "store-1",
      documentId: "doc-1",
      fetchImpl: vi.fn<typeof fetch>(async () =>
        jsonResponse({ error: { code: "stale_document", message: "Select the passage again." } }, 409),
      ),
    });
    await expect(client.load({ scope: "document", filter: "all" })).rejects.toThrow(
      "Select the passage again.",
    );
  });
});
