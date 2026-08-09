import { describe, expect, it, vi } from "vitest";

import type { CoworkCapturedActionSnapshot } from "../targets";
import { HttpCoworkTruthAnalysisClient } from "./HttpCoworkTruthAnalysisClient";

const jsonResponse = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const capture: CoworkCapturedActionSnapshot = {
  schema: "wb.cowork.action-snapshot/v1",
  captureId: "capture-1",
  storeId: "store-1",
  documentId: "doc-1",
  capturedAt: "2026-08-09T12:00:00Z",
  editGeneration: 7,
  ydocGenerationSha256: "a".repeat(64),
  snapshotBase64: "c25hcHNob3Q=",
  snapshotSha256: "b".repeat(64),
  stateVectorBase64: "dmVjdG9y",
  stateVectorSha256: "c".repeat(64),
  structuredHeadSha256: "d".repeat(64),
  projectionMarkdown: "A selected factual passage.",
  projectionSha256: "e".repeat(64),
  projectionReceiptId: "receipt-1",
  target: {
    source: "current_selection",
    label: "Selected passage",
    wordCount: 4,
    proseMirrorRange: { from: 1, to: 28 },
    selector: {
      kind: "text_quote",
      exact: "A selected factual passage.",
      prefix: "",
      suffix: "",
      start: 0,
      end: 27,
    },
    targetTextSha256: "f".repeat(64),
  },
};

const runPayload = {
  schema: "wb.cowork.truth-analysis-run/v1",
  analysis_run_id: "run-1",
  store_id: "store-1",
  document_id: "doc-1",
  status: "completed",
  target_choice: "current_selection",
  target_label: "Selected passage",
  captured_at: "2026-08-09T12:00:00Z",
  structured_head_sha256: "d".repeat(64),
  projection_sha256: "e".repeat(64),
  execution: {
    provider_id: "claude-code",
    model_id: "sonnet",
    provider_label: "Claude Code",
    model_label: "Sonnet",
  },
  source_coverage: [
    {
      source: "selected_passage",
      status: "searched",
      detail: "The captured passage was analyzed.",
      external_egress: false,
    },
    {
      source: "web",
      status: "not_searched",
      detail: null,
      external_egress: false,
    },
  ],
  limitations: ["No web search was performed."],
  candidates: [
    {
      candidate_id: "candidate-1",
      canonical_sha256: "1".repeat(64),
      status: "pending",
      decision: null,
      proposition: "A bounded proposition.",
      claim_kind: "factual",
      confidence_extraction: 0.84,
      expression: {
        role: "paraphrase",
        quote: "A selected factual passage.",
        selector: {
          exact: "A selected factual passage.",
          prefix: "",
          suffix: "",
          start: 0,
          end: 27,
        },
      },
      existing_claim_match: {
        claim_id: "claim-1",
        proposition: "A bounded proposition.",
        relationship: "equivalent",
        confidence: 0.91,
        rationale: "The propositions have the same meaning.",
      },
      evidence: [
        {
          evidence_candidate_id: "evidence-candidate-1",
          source_kind: "truth_span",
          attachable: true,
          relationship: "supports",
          quote: "Recorded support.",
          source_locator: "truth://evidence/1",
          source_title: "Recorded evidence",
          trust_class: "human_authored",
          integrity_state: "recorded",
          rationale: "Directly supports the claim.",
        },
        {
          evidence_candidate_id: "evidence-candidate-2",
          source_kind: "web_fetch",
          attachable: true,
          relationship: "partially_supports",
          quote: "Captured web support.",
          source_locator: "https://research.example.test/article",
          source_title: "Research source",
          trust_class: "external_quarantined",
          integrity: {
            status: "captured_runtime",
            content_sha256: "2".repeat(64),
            fetch_id: "fetch-1",
            capture: {
              text_truncated: true,
              captured_text_bytes: 65_536,
              extracted_text_bytes: 90_000,
              captured_text_sha256: "2".repeat(64),
              full_extracted_text_sha256: "3".repeat(64),
              maximum_captured_text_bytes: 65_536,
            },
          },
          rationale: "The fetched source supports part of the claim.",
        },
      ],
      source_coverage: [],
      limitations: [],
    },
  ],
  error: null,
  created_at: "2026-08-09T12:00:00Z",
  finished_at: "2026-08-09T12:00:05Z",
};

const capabilitiesPayload = {
  ok: true,
  schema: "wb.cowork.truth-analysis-capabilities/v1",
  required_cost_control: {
    enforcement_class: "hard_ceiling",
    scope: "worker_model_session",
    maximum_usd_per_model_session: 2,
  },
  research_cost_control: {
    enforcement_class: "unavailable",
    scope: "web_search_and_fetch",
    ceiling_usd: null,
    basis: "research_provider_cost_not_enforced",
  },
  providers: [
    {
      provider_id: "claude-code",
      analysis_available: true,
      unavailable_reason: null,
      applies_to_all_models: true,
      cost_control: {
        enforcement_class: "hard_ceiling",
        ceiling_usd_per_worker_session: 2,
        basis: "claude_code_max_budget_usd",
      },
    },
    {
      provider_id: "codex",
      analysis_available: false,
      unavailable_reason:
        "Truth analysis requires a provider-enforced hard spending ceiling.",
      applies_to_all_models: true,
      cost_control: {
        enforcement_class: "unavailable",
        ceiling_usd_per_worker_session: null,
        basis: "codex_worker_has_no_budget_enforcement",
      },
    },
  ],
};

describe("HttpCoworkTruthAnalysisClient", () => {
  it("loads server-attested provider cost eligibility fail-closed", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      jsonResponse(capabilitiesPayload),
    );
    const client = new HttpCoworkTruthAnalysisClient({
      storeId: "store-1",
      documentId: "doc-1",
      fetchImpl,
    });

    await expect(client.loadCapabilities()).resolves.toMatchObject({
      requiredCostControl: {
        enforcementClass: "hard_ceiling",
        scope: "worker_model_session",
        maximumUsdPerModelSession: 2,
      },
      researchCostControl: {
        enforcementClass: "unavailable",
        scope: "web_search_and_fetch",
      },
      providers: [
        expect.objectContaining({
          providerId: "claude-code",
          analysisAvailable: true,
        }),
        expect.objectContaining({
          providerId: "codex",
          analysisAvailable: false,
        }),
      ],
    });
    expect(String(fetchImpl.mock.calls[0][0])).toBe(
      "/api/truth/doc/doc-1/truth/analysis-capabilities?store_id=store-1",
    );
  });

  it("rejects malformed cost eligibility instead of enabling analysis", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      jsonResponse({
        ...capabilitiesPayload,
        providers: [{
          ...capabilitiesPayload.providers[0],
          cost_control: {
            ...capabilitiesPayload.providers[0].cost_control,
            enforcement_class: "estimate",
          },
        }],
      }),
    );
    const client = new HttpCoworkTruthAnalysisClient({
      storeId: "store-1",
      documentId: "doc-1",
      fetchImpl,
    });

    await expect(client.loadCapabilities()).rejects.toThrow(
      "invalid provider capabilities",
    );
  });

  it("starts one run with the exact frozen capture and shared execution IDs", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () => jsonResponse(runPayload, 201));
    const client = new HttpCoworkTruthAnalysisClient({
      storeId: "store-1",
      documentId: "doc-1",
      fetchImpl,
    });

    const result = await client.start({
      targetChoice: "current_selection",
      capture,
      execution: {
        providerId: "claude-code",
        modelId: "sonnet",
        providerLabel: "Claude Code",
        modelLabel: "Sonnet",
      },
    });

    expect(result.analysisRunId).toBe("run-1");
    expect(fetchImpl).toHaveBeenCalledOnce();
    const [input, init] = fetchImpl.mock.calls[0];
    expect(String(input)).toBe(
      "/api/truth/doc/doc-1/truth/analysis-runs?store_id=store-1",
    );
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({
      capture,
      execution: {
        provider_id: "claude-code",
        model_id: "sonnet",
      },
    });
  });

  it("restores the current run and preserves server-reported coverage", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () => jsonResponse({ run: runPayload }));
    const client = new HttpCoworkTruthAnalysisClient({
      storeId: "store-1",
      documentId: "doc-1",
      fetchImpl,
    });

    const run = await client.loadCurrent();

    expect(run?.candidates[0]).toMatchObject({
      candidateId: "candidate-1",
      status: "pending",
      existingClaimMatch: {
        claimId: "claim-1",
        relationship: "equivalent",
      },
    });
    expect(run?.candidates[0].evidence[0]).toMatchObject({
      evidenceCandidateId: "evidence-candidate-1",
      attachable: true,
      relationship: "supports",
    });
    expect(run?.candidates[0].evidence[1]).toMatchObject({
      evidenceCandidateId: "evidence-candidate-2",
      sourceKind: "web_fetch",
      attachable: true,
      integrityState: "captured_runtime",
      capture: {
        textTruncated: true,
        capturedTextBytes: 65_536,
        extractedTextBytes: 90_000,
      },
    });
    expect(run?.sourceCoverage).toEqual([
      expect.objectContaining({ source: "selected_passage", status: "searched" }),
      expect.objectContaining({ source: "web", status: "not_searched" }),
    ]);
  });

  it("sends distinct hash-bound save, connect, and dismiss decisions", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async (_input, init) => {
      const body = JSON.parse(String(init?.body)) as { decision: string };
      return jsonResponse({
        ok: true,
        analysis_run_id: "run-1",
        candidate_id: "candidate-1",
        candidate_status: body.decision === "dismiss" ? "dismissed" : "saved",
        claim_id: body.decision === "dismiss" ? null : "claim-1",
        expression_id: body.decision === "dismiss" ? null : "expression-1",
      });
    });
    const client = new HttpCoworkTruthAnalysisClient({
      storeId: "store-1",
      documentId: "doc-1",
      fetchImpl,
    });

    await client.decideCandidate({
      analysisRunId: "run-1",
      candidateId: "candidate-1",
      decision: "save_as_proposed",
      expectedCanonicalSha256: "1".repeat(64),
      edits: {
        proposition: "A bounded proposition.",
        claimKind: "factual",
        expressionRole: "paraphrase",
        evidenceCandidateIds: ["evidence-candidate-1"],
      },
    });
    await client.decideCandidate({
      analysisRunId: "run-1",
      candidateId: "candidate-1",
      decision: "connect_existing",
      expectedCanonicalSha256: "1".repeat(64),
      existingClaimId: "claim-1",
      edits: {
        proposition: "A bounded proposition.",
        claimKind: "factual",
        expressionRole: "paraphrase",
        evidenceCandidateIds: [],
      },
    });
    await client.decideCandidate({
      analysisRunId: "run-1",
      candidateId: "candidate-1",
      decision: "dismiss",
      expectedCanonicalSha256: "1".repeat(64),
    });

    const bodies = fetchImpl.mock.calls.map(([, init]) =>
      JSON.parse(String(init?.body)),
    );
    expect(bodies).toEqual([
      {
        decision: "save_as_proposed",
        expected_canonical_sha256: "1".repeat(64),
        edits: {
          proposition: "A bounded proposition.",
          claim_kind: "factual",
          expression_role: "paraphrase",
          evidence_candidate_ids: ["evidence-candidate-1"],
        },
      },
      {
        decision: "connect_existing",
        expected_canonical_sha256: "1".repeat(64),
        existing_claim_id: "claim-1",
        edits: {
          proposition: "A bounded proposition.",
          claim_kind: "factual",
          expression_role: "paraphrase",
          evidence_candidate_ids: [],
        },
      },
      {
        decision: "dismiss",
        expected_canonical_sha256: "1".repeat(64),
      },
    ]);
  });

  it("rejects malformed candidates instead of making them actionable", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      jsonResponse({
        ...runPayload,
        candidates: [
          {
            ...runPayload.candidates[0],
            status: "unexpected",
          },
        ],
      }),
    );
    const client = new HttpCoworkTruthAnalysisClient({
      storeId: "store-1",
      documentId: "doc-1",
      fetchImpl,
    });

    await expect(client.loadRun("run-1")).rejects.toThrow(
      "invalid candidate status",
    );
  });

  it("does not reinterpret malformed target identity or source coverage", async () => {
    const payloads = [
      { ...runPayload, target_choice: "scope" },
      {
        ...runPayload,
        source_coverage: {
          mode: "legacy",
          web_search_performed: false,
        },
      },
    ];
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      jsonResponse(payloads.shift()),
    );
    const client = new HttpCoworkTruthAnalysisClient({
      storeId: "store-1",
      documentId: "doc-1",
      fetchImpl,
    });

    await expect(client.loadRun("run-1")).rejects.toThrow(
      "invalid target choice",
    );
    await expect(client.loadRun("run-1")).rejects.toThrow(
      "invalid source coverage",
    );
  });
});
