import { describe, expect, it, vi } from "vitest";

import { HttpCoworkDocClient } from "./HttpCoworkDocClient";

describe("HttpCoworkDocClient Verify setup", () => {
  it("sends the exact effective activation as a compare-and-set precondition", async () => {
    const configuration = {
      schema: "work-buddy.cowork-verify-configuration/v1",
      document_id: "doc-1",
      coordination: {
        required: true,
        selection: "explicit_provider_and_model_at_run_start",
        content_boundary: "complete_permitted_frozen_document",
        egress_class: "account_backed_agent",
        external_egress: true,
        cost_ceiling_usd_per_worker: 2,
        separate_reviser_for_findings: true,
      },
      criteria: [],
    };
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      new Response(JSON.stringify({ ok: true, configuration }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const client = new HttpCoworkDocClient({
      documentId: "doc-1",
      storeId: "store-1",
      fetchImpl: fetchImpl as typeof fetch,
    });

    await expect(
      client.setVerifyCriterionEnabled(
        "terminology_exact_match",
        false,
        "activation-1",
      ),
    ).resolves.toEqual(configuration);

    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe(
      "/api/truth/doc/doc-1/verify/criteria/terminology_exact_match?store_id=store-1",
    );
    expect(init?.method).toBe("PATCH");
    expect(JSON.parse(String(init?.body))).toEqual({
      enabled: false,
      expected_activation_id: "activation-1",
    });
  });

  it("keeps a Co-think lifecycle action bound to the exact item hash", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const client = new HttpCoworkDocClient({
      documentId: "doc-1",
      storeId: "store-1",
      fetchImpl: fetchImpl as typeof fetch,
    });

    await client.actOnCothink("item-1", "park", "item-sha");

    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe(
      "/api/truth/doc/doc-1/cothink/items/item-1/actions?store_id=store-1",
    );
    expect(JSON.parse(String(init?.body))).toEqual({
      action: "park",
      canonical_sha256: "item-sha",
    });
  });

  it("routes Discuss with the exact Co-think item hash into Chat", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      new Response(
        JSON.stringify({
          ok: true,
          conversation_id: "conversation-1",
          message_id: "message-1",
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    const client = new HttpCoworkDocClient({
      documentId: "doc-1",
      storeId: "store-1",
      fetchImpl: fetchImpl as typeof fetch,
    });

    await expect(
      client.discussCothink("item-1", "item-sha"),
    ).resolves.toEqual({
      conversationId: "conversation-1",
      messageId: "message-1",
    });
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe(
      "/api/truth/doc/doc-1/cothink/items/item-1/actions?store_id=store-1",
    );
    expect(JSON.parse(String(init?.body))).toEqual({
      action: "discuss",
      canonical_sha256: "item-sha",
    });
  });

  it("maps only the typed safe Verify inspection projection", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      new Response(
        JSON.stringify({
          ok: true,
          detail: {
            schema: "work-buddy.cowork-verify-run-inspection/v1",
            run_id: "run-1",
            action: {
              action_snapshot_id: "action-1",
              structured_head_sha256: "head-sha",
              target_kind: "document",
              context_boundary: { kind: "complete_frozen_document" },
              egress_boundary: { class: "account_backed_agent" },
            },
            plan: {
              plan_snapshot_id: "plan-1",
              canonical_sha256: "plan-sha",
              definition: { checks: ["check-1"] },
            },
            checks: [
              {
                check_execution_id: "execution-1",
                status: "completed",
                mechanism: "deterministic",
                definition: {
                  stable_key: "terminology_exact_match",
                  version: 1,
                  title: "Terminology exact match",
                  limitations: ["Exact strings only."],
                },
              },
            ],
            results: [
              {
                evaluation_result_id: "result-1",
                kind: "conforming",
                message: "No configured term was found.",
                dispositions: [
                  {
                    decision: "suppress",
                    rationale: "Quiet clean outcome.",
                    policy_snapshot_sha256: "policy-sha",
                  },
                ],
                lineage: [],
              },
            ],
            coordination: [
              {
                job_id: "job-1",
                role: "coordinator",
                status: "completed",
                provider: "codex",
                model: "gpt-5.6",
                egress_class: "account_backed_agent",
                cost_ceiling_usd: 2,
                execution_plan: {
                  schema:
                    "work-buddy.cowork-verify-execution-disclosure/v1",
                  authoritative: true,
                  checker: {
                    execution_class: "in_process",
                    mechanism: "deterministic_exact_match",
                    model_call: false,
                    external_egress: false,
                    content_boundary: "captured_target",
                  },
                  coordination: {
                    execution_class: "account_backed_agent",
                    selection: {
                      mode: "explicit_at_run_start",
                      provider_id: "codex",
                      model_id: "gpt-5.6",
                      provider_label: "Codex",
                      model_label: "GPT-5.6",
                    },
                    content_boundary: "entire_frozen_document",
                    external_egress: true,
                    fallback: {
                      provider_model_fallback: false,
                      failure_mode: "fail_closed",
                    },
                    worker_sessions: {
                      initial: 1,
                      maximum: 3,
                      conditional_roles: [
                        "reviser",
                        "post_revision_coordinator",
                      ],
                    },
                    cost_control: {
                      provider_id: "codex",
                      enforcement_class: "unavailable",
                      ceiling_usd_per_worker_session: null,
                      basis: "codex_worker_has_no_budget_enforcement",
                    },
                    provider_cost_controls: [],
                  },
                },
                error: null,
                raw_output: "must never be mapped",
              },
            ],
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    const client = new HttpCoworkDocClient({
      documentId: "doc-1",
      storeId: "store-1",
      fetchImpl: fetchImpl as typeof fetch,
    });

    const detail = await client.inspectVerifyRun("run-1");

    expect(detail.runId).toBe("run-1");
    expect(detail.checks[0]?.definition.version).toBe(1);
    expect(detail.results[0]?.dispositions[0]?.decision).toBe("suppress");
    expect(
      detail.coordination[0]?.executionPlan?.coordination.costControl,
    ).toMatchObject({
      providerId: "codex",
      enforcementClass: "unavailable",
    });
    expect(detail.coordination[0]).not.toHaveProperty("raw_output");
    expect(fetchImpl.mock.calls[0]?.[0]).toBe(
      "/api/truth/doc/doc-1/verify/runs/run-1?store_id=store-1",
    );
  });

  it("saves a user-authored criterion as an unavailable draft", async () => {
    const configuration = {
      schema: "work-buddy.cowork-verify-configuration/v1",
      document_id: "doc-1",
      coordination: {
        required: true,
        selection: "explicit_provider_and_model_at_run_start",
        content_boundary: "complete_permitted_frozen_document",
        egress_class: "account_backed_agent",
        external_egress: true,
        cost_ceiling_usd_per_worker: 2,
        separate_reviser_for_findings: true,
      },
      criteria: [],
    };
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      new Response(JSON.stringify({ ok: true, configuration }), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const client = new HttpCoworkDocClient({
      documentId: "doc-1",
      storeId: "store-1",
      fetchImpl: fetchImpl as typeof fetch,
    });

    await client.createVerifyCriterionDraft({
      title: "State the positive claim",
      description: "Prefer direct positive descriptions.",
      evaluationInstructions: "Identify negative-definition framing.",
      limitations: ["Negation can be necessary."],
    });

    const [url, init] = fetchImpl.mock.calls[0] ?? [];
    expect(url).toBe(
      "/api/truth/doc/doc-1/verify/criteria/drafts?store_id=store-1",
    );
    expect(JSON.parse(String(init?.body))).toEqual({
      title: "State the positive claim",
      description: "Prefer direct positive descriptions.",
      evaluation_instructions: "Identify negative-definition framing.",
      limitations: ["Negation can be necessary."],
    });
  });
});
