import { afterEach, describe, expect, it, vi } from "vitest";

import { resetLocalIdentityForTests } from "../../security/localIdentity";
import {
  JournalProfileConfigurationClient,
  cloneProfileDraft,
  editProfileDraft,
  type JournalProfileRevisionRecord,
} from "./profileConfiguration";

const json = (body: unknown, status = 200): Response => new Response(JSON.stringify(body), {
  status,
  headers: { "Content-Type": "application/json" },
});

const principal = {
  actor: {
    schema: "wb.actor-ref/v1",
    issuer_authority_id: "wia_1234567890",
    subject: "wactor_1234567890",
    kind: "human",
    tenant_scope_id: "wts_1234567890",
  },
  origin: window.location.origin,
  audience: "work-buddy-dashboard",
  session_expires_at: Date.now() / 1000 + 600,
  rotation_due_at: Date.now() / 1000 + 300,
  assurance: "enrolled_local_session",
};

const editableProfile = {
  profileId: "user.focus", profileRevision: 3, formatVersion: 1,
  name: "Focus", description: "A focused reflection.", profileDigest: "digest",
  createdBy: "person:test", createdAt: "1970-01-01T00:00:00Z", supersedesRevision: 2,
  editable: true,
  modules: [{
    slotId: "check", ordinal: 0, required: false,
    moduleInstanceId: "user.focus.check", moduleInstanceVersion: 5,
    moduleTypeId: "field_group", moduleTypeVersion: 1, label: "Check",
    settings: {}, behaviorId: "human_value", behaviorVersion: 1,
    scheduleKind: "always", schedule: {}, fields: [{
      slotId: "clarity", fieldId: "user.focus.clarity", fieldDefinitionVersion: 7,
      owner: "user", stableKey: "clarity", label: "Clarity", description: "",
      valueKind: "scale", unit: null, constraints: {},
      functionId: "function.focus", functionVersion: 1, behaviorId: "human_value",
      behaviorVersion: 1, privacyClass: "private", searchMode: "structured_only",
      disclosurePolicyId: "private_default/v1", prompt: {
        promptId: "user.focus.clarity.prompt", promptVersion: 4,
        wording: "Focus?", helpText: "", requiredness: "optional",
        scheduleKind: "always", schedule: {},
      },
    }],
  }],
} satisfies JournalProfileRevisionRecord;

afterEach(() => resetLocalIdentityForTests());

describe("JournalProfileConfigurationClient", () => {
  it("binds save and future activation to separate exact human gestures", async () => {
    const gestures: Record<string, unknown>[] = [];
    const writes: { url: string; body: Record<string, unknown>; headers: Headers }[] = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/local-identity/session/csrf") {
        return json({ ok: true, authenticated: true, principal, csrf_token: "csrf" });
      }
      if (url === "/api/local-identity/gestures") {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        gestures.push(body);
        return json({ ok: true, gesture: {
          token: `gesture-${gestures.length}`,
          action: body.action,
          subject_sha256: "a".repeat(64),
          context_sha256: body.context_sha256,
          expires_at: Date.now() / 1000 + 30,
        } });
      }
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      writes.push({ url, body, headers: new Headers(init?.headers) });
      if (url.endsWith("/activate")) {
        return json({ ok: true, activation: {
          profileId: "user.focus", profileRevision: 4,
          effectiveLocalDate: "2099-01-01", activationRevision: 2,
        } });
      }
      return json({ ok: true, profile: {
        profileId: "user.focus", profileRevision: 4,
        profileDigest: "digest", activationRevision: 1,
      } }, 201);
    });
    const client = new JournalProfileConfigurationClient(fetchImpl);
    const draft = editProfileDraft(editableProfile);

    const saved = await client.save(draft);
    await client.activate({
      profileId: saved.profileId,
      profileRevision: saved.profileRevision,
      expectedActivationRevision: saved.activationRevision,
      effectiveLocalDate: "2099-01-01",
    });

    expect(gestures.map((item) => [item.action, item.subject])).toEqual([
      ["journal.profile.save", "journal-profile:user.focus"],
      ["journal.profile.activate", "journal-profile:user.focus:4"],
    ]);
    expect(gestures.every((item) => /^[0-9a-f]{64}$/u.test(String(item.context_sha256)))).toBe(true);
    expect(writes.map((item) => item.headers.get("X-WB-Gesture"))).toEqual([
      "gesture-1", "gesture-2",
    ]);
    expect(writes[1]?.body).toMatchObject({
      expectedActivationRevision: 1,
      effectiveLocalDate: "2099-01-01",
    });
    expect(writes[0]?.body).toMatchObject({
      draft: {
        profileId: "user.focus",
        expectedRevision: 3,
        modules: [{
          moduleInstanceId: "user.focus.check",
          expectedVersion: 5,
          fields: [{
            fieldId: "user.focus.clarity",
            expectedVersion: 7,
            stableKey: "clarity",
            prompt: {
              promptId: "user.focus.clarity.prompt",
              expectedVersion: 4,
            },
          }],
        }],
      },
    });
  });

  it("clones a profile into new stable module, field, and prompt identities", () => {
    const clone = cloneProfileDraft(editableProfile);

    expect(clone.profileId).not.toBe(editableProfile.profileId);
    expect(clone.expectedRevision).toBe(0);
    expect(clone.modules[0]?.slotId).not.toBe(editableProfile.modules[0]?.slotId);
    expect(clone.modules[0]?.moduleInstanceId).not.toBe(editableProfile.modules[0]?.moduleInstanceId);
    expect(clone.modules[0]?.expectedVersion).toBe(0);
    expect(clone.modules[0]?.fields[0]?.fieldId).not.toBe(editableProfile.modules[0]?.fields[0]?.fieldId);
    expect(clone.modules[0]?.fields[0]?.stableKey).not.toBe(editableProfile.modules[0]?.fields[0]?.stableKey);
    expect(clone.modules[0]?.fields[0]?.expectedVersion).toBe(0);
    expect(clone.modules[0]?.fields[0]?.prompt?.promptId).not.toBe(
      editableProfile.modules[0]?.fields[0]?.prompt?.promptId,
    );
    expect(clone.modules[0]?.fields[0]?.prompt?.expectedVersion).toBe(0);
    expect(clone.modules[0]?.fields[0]?.label).toBe("Clarity");
    expect(clone.modules[0]?.fields[0]?.functionId).toBe("function.focus");
    expect(clone.modules[0]?.fields[0]?.functionVersion).toBe(1);
  });

  it("edits a profile with stable identities and current expected versions", () => {
    const edit = editProfileDraft(editableProfile);

    expect(edit).toMatchObject({
      profileId: "user.focus",
      expectedRevision: 3,
      modules: [{
        slotId: "check",
        moduleInstanceId: "user.focus.check",
        expectedVersion: 5,
        fields: [{
          slotId: "clarity",
          fieldId: "user.focus.clarity",
          expectedVersion: 7,
          owner: "user",
          stableKey: "clarity",
          prompt: {
            promptId: "user.focus.clarity.prompt",
            expectedVersion: 4,
          },
        }],
      }],
    });
  });

  it("does not turn a read-only catalog profile into an edit draft", () => {
    expect(() => editProfileDraft({ ...editableProfile, editable: false })).toThrow(
      "Fork it before editing",
    );
  });
});
