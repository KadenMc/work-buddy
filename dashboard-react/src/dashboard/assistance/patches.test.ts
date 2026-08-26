import { webcrypto } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { JsonObject } from "../contributions/contracts";
import { asAppId, asViewId, asWidgetInstanceId, asWidgetTypeId } from "../contributions/contracts";
import type { AssistedDraftPatch } from "./contracts";
import { planPatch, planUndo, validatePatch } from "./patches";
import { assistedForms, discloseSnapshot, pathKey, snapshotHash, validateOperations } from "./schema";

const form = assistedForms["task-create"];
const identity = { profileId: "profile", workspaceId: "workspace", appId: asAppId("wb.tasks"), viewId: asViewId("wb.tasks.main"), instanceId: asWidgetInstanceId("task-quick-add"), widgetTypeId: asWidgetTypeId("wb.tasks.quick-add"), draftName: "task-create", scopeKey: "view" };
const binding = { identity, assistantSessionId: "as-test", conversationId: "conversation-test", form };

async function patch(baseSnapshot: JsonObject = { title: "Original", summary: "", next_action: "" }): Promise<AssistedDraftPatch> {
  return { protocol: "wb.assisted-draft.patch/v1", identity, assistantSessionId: binding.assistantSessionId, conversationId: binding.conversationId, schema: form.schema, baseDraftRevision: 3, baseSnapshot, baseSnapshotHash: await snapshotHash(baseSnapshot), patchId: "patch-1", operations: [{ op: "set", path: ["title"], value: "Suggested" }, { op: "set", path: ["summary"], value: "A useful summary" }, { op: "set", path: ["next_action"], value: "Read the plan" }] };
}

beforeEach(() => { vi.stubGlobal("crypto", webcrypto); });
afterEach(() => { vi.unstubAllGlobals(); });

describe("widget-native assisted patch laws", () => {
  it("discloses only manifest fields, preserving all host-only metadata", () => {
    const value = { title: "Task", summary: "Details", batch_lines: ["one", "two"], proposal_ref: { threadId: "th-123", draftFingerprint: "hash" }, proposal_pending: { clientMutationId: "key", parameters: "private" }, password: "secret" };
    expect(discloseSnapshot(form, value)).toEqual({ title: "Task", summary: "Details" });
    expect(value.proposal_pending.parameters).toBe("private");
  });

  it("stale revision applies only unchanged, unfocused fields in one revision", async () => {
    const proposal = await patch();
    const current = { value: { ...proposal.baseSnapshot, title: "Human title", proposal_ref: { threadId: "th-123" }, batch_lines: [] }, revision: 9 };
    const plan = planPatch(proposal, form, current, new Set([pathKey(["summary"])]));
    expect(plan.value).toEqual({ ...current.value, next_action: "Read the plan" });
    expect(plan.receipt).toMatchObject({ status: "partial", resultingRevision: 10, appliedFields: [["next_action"]], pendingFields: [{ path: ["title"], reason: "user_changed" }, { path: ["summary"], reason: "focused" }] });
  });

  it("conditional Undo keeps subsequent human edits and focused controls", async () => {
    const proposal = await patch();
    const applied = planPatch(proposal, form, { value: proposal.baseSnapshot, revision: 3 }, new Set());
    const current = { value: { ...applied.value, title: "Edited after assistant" }, revision: 5 };
    const undone = planUndo(proposal.patchId, applied.changes, current, new Set([pathKey(["summary"])]));
    expect(undone.value).toEqual({ title: "Edited after assistant", summary: "A useful summary", next_action: "" });
    expect(undone.receipt).toMatchObject({ status: "undone", resultingRevision: 6, appliedFields: [["next_action"]] });
    expect(undone.receipt.pendingFields).toHaveLength(2);
  });

  it("remove resets only optional fields and Undo restores absent properties", async () => {
    const proposal = { ...await patch({ summary: "old" }), operations: [{ op: "remove" as const, path: ["summary"] }] };
    validateOperations(form, proposal.operations);
    const applied = planPatch(proposal, form, { value: { summary: "old" }, revision: 3 }, new Set());
    expect(applied.value).toEqual({ summary: "" });
    const missing = { ...await patch({}), operations: [{ op: "set" as const, path: ["summary"], value: "new" }] };
    const added = planPatch(missing, form, { value: {}, revision: 3 }, new Set());
    expect(planUndo(missing.patchId, added.changes, { value: added.value, revision: 4 }, new Set()).value).toEqual({});
  });

  it("validates full identity/schema/hash and rejects before any partial mutation", async () => {
    const wire = await patch();
    expect(await validatePatch(wire, binding)).toEqual(wire);
    await expect(validatePatch({ ...wire, identity: { ...identity, workspaceId: "other" } }, binding)).rejects.toThrow("different draft");
    await expect(validatePatch({ ...wire, baseSnapshotHash: "0".repeat(64) }, binding)).rejects.toThrow("hash mismatch");
    await expect(validatePatch({ ...wire, operations: [...wire.operations, { op: "set", path: ["__proto__", "polluted"], value: true }] }, binding)).rejects.toThrow();
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
  });

  it.each([
    { op: "submit", path: ["title"] },
    { op: "set", path: [], value: {} },
    { op: "set", path: ["*"], value: "wildcard" },
    { op: "set", path: ["password"], value: "secret" },
    { op: "set", path: ["batch_lines"], value: [] },
    { op: "set", path: ["proposal_ref"], value: {} },
    { op: "set", path: ["title"], value: 9 },
    { op: "set", path: ["title"], value: "x".repeat(1001) },
    { op: "remove", path: ["title"] },
  ])("rejects malformed/secret/unallowlisted operation %#", (operation) => {
    expect(() => validateOperations(form, [{ op: "set", path: ["summary"], value: "valid" }, operation])).toThrow();
  });

  it("Jobs uses the same canonical schema and refuses secret parameter JSON", () => {
    const jobs = assistedForms["job-create"];
    expect(discloseSnapshot(jobs, { name: "Weekly review", params: '{"day":"Monday"}', jitter_seconds: 0 })).toEqual({ name: "Weekly review", params: '{"day":"Monday"}', jitter_seconds: 0 });
    expect(() => validateOperations(jobs, [{ op: "set", path: ["name"], value: "a".repeat(64) }])).not.toThrow();
    expect(() => validateOperations(jobs, [{ op: "set", path: ["name"], value: "a".repeat(65) }])).toThrow();
    expect(() => discloseSnapshot(jobs, { params: '{"nested":{"api_key":"secret"}}' })).toThrow("Secret parameters");
    expect(() => validateOperations(jobs, [{ op: "set", path: ["jitter_seconds"], value: -2 }])).toThrow();
  });

  it("a user-reviewed suggestion can replace a changed field, never a focused field", async () => {
    const proposal = await patch();
    const current = { value: { ...proposal.baseSnapshot, title: "Human title" }, revision: 5 };
    const paths = new Set([pathKey(["title"])]);
    expect(planPatch(proposal, form, current, new Set(), paths).value.title).toBe("Suggested");
    expect(planPatch(proposal, form, current, paths, paths).value.title).toBe("Human title");
  });
});
