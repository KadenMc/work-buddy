import { describe, expect, it, vi } from "vitest";
import { ChatExecutionSelectionError } from "../widget-library/chat";
import { asSettingId, type EffectiveSettingValue } from "./contracts";
import { SettingsExecutionProfileProvider } from "./SettingsExecutionProfileProvider";

const settingId = asSettingId("wb.dashboard.chat-execution-default");
const sonnet = { provider_id: "claude-code", model_id: "sonnet" };
const codex = { provider_id: "codex", model_id: "fixture-model" };
const catalog = { providers: [
  { id: "claude-code", label: "Claude Code", available: true, models: [{ id: "sonnet", label: "Sonnet", available: true }] },
  { id: "codex", label: "Codex", available: true, models: [{ id: "fixture-model", label: "Fixture model", available: true }] },
] };
const value = (pair = sonnet, revision = "value:0"): EffectiveSettingValue => ({ settingId, scope: { kind: "profile", subjectId: "default" }, effectiveValue: pair, source: revision === "value:0" ? "default" : "profile", isModified: revision !== "value:0", revision, diagnostics: [] });
const raw = (record: EffectiveSettingValue) => ({ setting_id: record.settingId, scope: record.scope, effective_value: record.effectiveValue, source: record.source, is_modified: record.isModified, revision: record.revision });
const envelope = (record: EffectiveSettingValue) => ({ schema_version: 1, registry_revision: "settings-registry:6", value: raw(record) });

function setup(fetchImpl: typeof fetch) {
  let current = value();
  let readOnly = false;
  const adopt = vi.fn((next: EffectiveSettingValue) => { current = next; });
  const provider = new SettingsExecutionProfileProvider({ settingId, getValue: () => current, getReadOnly: () => readOnly, adoptValue: adopt, fetchImpl });
  return { provider, adopt, setValue: (next: EffectiveSettingValue) => { current = next; }, setReadOnly: (next: boolean) => { readOnly = next; } };
}

describe("SettingsExecutionProfileProvider", () => {
  it("reads the catalog without creating a conversation or writing any setting", async () => {
    const fetcher = vi.fn(async () => Response.json(catalog));
    const { provider, adopt } = setup(fetcher);
    const snapshot = await provider.load(settingId);
    expect(snapshot.selection).toEqual({ providerId: "claude-code", modelId: "sonnet", providerLabel: "Claude Code", modelLabel: "Sonnet", revision: "value:0" });
    expect(fetcher).toHaveBeenCalledExactlyOnceWith("/api/settings/execution-catalog", { headers: { Accept: "application/json" } });
    expect(adopt).not.toHaveBeenCalled();
  });

  it("writes only the exact atomic pair through the revisioned Settings API", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (!init?.method) return Response.json(catalog);
      expect(String(input)).toBe(`/api/settings/values/${settingId}`);
      expect(init.method).toBe("PATCH");
      expect(JSON.parse(String(init.body))).toEqual({ scope: "profile", value: codex, expected_revision: "value:0" });
      return Response.json(envelope(value(codex, "value:1")));
    });
    const { provider, adopt } = setup(fetcher);
    await provider.load(settingId);
    const next = await provider.select(settingId, { providerId: "codex", modelId: "fixture-model", expectedRevision: "value:0" });
    expect(next.selection).toMatchObject({ providerId: "codex", modelLabel: "Fixture model", revision: "value:1" });
    expect(adopt).toHaveBeenCalledOnce();
  });

  it("rejects conflicts while returning the authoritative pair instead of false success", async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => init?.method ? Response.json({ error: "revision_conflict", message: "Changed elsewhere.", value: raw(value(codex, "value:2")) }, { status: 409 }) : Response.json(catalog));
    const { provider } = setup(fetcher);
    await provider.load(settingId);
    await expect(provider.select(settingId, { providerId: "codex", modelId: "fixture-model", expectedRevision: "value:0" })).rejects.toMatchObject({ name: "ChatExecutionSelectionError", message: "Changed elsewhere.", authoritativeSnapshot: { selection: { revision: "value:2" } } });
  });

  it("surfaces failed writes and leaves the displayed default unchanged", async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => init?.method ? Response.json({ error: "provider_unavailable", message: "The provider could not be checked." }, { status: 503 }) : Response.json(catalog));
    const { provider, adopt } = setup(fetcher);
    await provider.load(settingId);
    await expect(provider.select(settingId, { providerId: "codex", modelId: "fixture-model", expectedRevision: "value:0" })).rejects.toBeInstanceOf(ChatExecutionSelectionError);
    expect(adopt).not.toHaveBeenCalled();
    expect((await provider.load(settingId)).selection.modelId).toBe("sonnet");
  });

  it("reads the latest Settings value after a delayed catalog response", async () => {
    let finish!: (response: Response) => void;
    const { provider, setValue } = setup(vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; })));
    const pending = provider.load(settingId);
    setValue(value(codex, "value:3"));
    finish(Response.json(catalog));
    expect((await pending).selection).toMatchObject({ providerId: "codex", revision: "value:3" });
  });

  it("respects read-only authority and keeps unavailable saved identities", async () => {
    const fetcher = vi.fn(async () => Response.json({ ...catalog, read_only: true }));
    const { provider, setValue, setReadOnly } = setup(fetcher);
    setValue(value({ provider_id: "missing", model_id: "old-model" }));
    setReadOnly(true);
    const snapshot = await provider.load(settingId);
    expect(snapshot.selection).toMatchObject({ providerId: "missing", modelId: "old-model" });
    expect(snapshot.readOnly).toBe(true);
    await expect(provider.select(settingId, { providerId: "codex", modelId: "fixture-model", expectedRevision: "value:0" })).rejects.toThrow("read-only");
    expect(fetcher).toHaveBeenCalledOnce();
    setReadOnly(false);
    expect((await provider.load(settingId)).readOnly).toBe(false);
  });

  it("refreshes discovery explicitly and fences an older catalog response", async () => {
    let finish!: (response: Response) => void;
    const fetcher = vi.fn<typeof fetch>().mockImplementationOnce(() => new Promise<Response>((resolve) => { finish = resolve; })).mockResolvedValueOnce(Response.json(catalog));
    const { provider } = setup(fetcher);
    const old = provider.load(settingId);
    provider.refresh(settingId);
    const latest = await provider.load(settingId);
    finish(Response.json({ providers: [] }));
    expect(await old).toEqual(latest);
    expect(fetcher.mock.calls[1]?.[0]).toBe("/api/settings/execution-catalog?refresh=1");
  });
});
