import { describe, expect, it } from "vitest";
import { parseTaskProposalEnvelope } from "./proposalApiContract";

const proposal = { thread_id: "th-1234abcd", proposal_event_id: 7, status: "ready", parameters: { task_text: "Review draft" }, origin: { kind: "journal", id: "capture-1" }, realization: null, href: "https://untrusted.example" };

describe("task proposal wire contract", () => {
  it("uses the numeric reviewed Thread event and constructs a trusted proposal URL", () => {
    expect(parseTaskProposalEnvelope({ ok: true, proposal })).toMatchObject({ proposal_event_id: 7, href: "/app/tasks?proposal=th-1234abcd" });
  });
  it("constructs the task handoff only from a validated structured receipt", () => {
    const result = parseTaskProposalEnvelope({ ok: true, proposal: { ...proposal, status: "realized", realization: { task_id: "t-1234abcd", receipt_id: "receipt-1", task_revision: 1, href: "//untrusted.example" } } });
    expect(result.realization?.href).toBe("/app/tasks?task=t-1234abcd");
  });
  it.each(["7", 0, -1, null, 1.5])("rejects an invalid event fence %s", (event) => {
    expect(() => parseTaskProposalEnvelope({ ok: true, proposal: { ...proposal, proposal_event_id: event } })).toThrow();
  });
  it("keeps typed missing/wrong-kind/superseded errors instead of guessing a task", () => {
    expect(() => parseTaskProposalEnvelope({ ok: true, proposal: { ...proposal, status: "unavailable", proposal_event_id: null, error: { code: "proposal_wrong_kind", message: "This Thread no longer proposes a task." } } })).toThrow("no longer proposes");
  });
  it("rejects a realized proposal without its receipt", () => {
    expect(() => parseTaskProposalEnvelope({ ok: true, proposal: { ...proposal, status: "realized" } })).toThrow("receipt");
  });
});
