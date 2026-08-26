import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import type { JsonObject, JsonSchemaReference } from "../contributions/contracts";
import { useWidgetAssistanceDeclaration, type WidgetDraftHandle } from "../drafts/WidgetDraftRuntime";
import { widgetDraftStorageKey, type WidgetDraftIdentity } from "../drafts/contracts";
import { HttpChatConversationProvider } from "../conversations/HttpChatConversationProvider";
import { exactHumanAuthorityHeaders } from "../../security/humanAuthority";
import { ConversationChat, type ChatSendInput } from "../../widget-library/chat";
import { Button } from "../../ui";
import type { AssistanceAvailability, AssistanceSession, AssistedDraftPatch, AssistedFormSchema, DraftPatchReceipt, PreparedDraftSnapshot } from "./contracts";
import { assistedForms, discloseSnapshot, equalJson, fieldFor, pathKey, readField, snapshotHash, validateFieldValue } from "./schema";
import { planPatch, planUndo, validatePatch, type FieldChange, type PatchPlan } from "./patches";
import "./assistance.css";

interface LiveDraft {
  readonly key: string;
  readonly identity: WidgetDraftIdentity;
  readonly schema: JsonSchemaReference;
  readonly form: AssistedFormSchema;
  title(): string;
  editable(): boolean;
  mounted(): boolean;
  generation(): number;
  snapshot(): { readonly value: JsonObject; readonly revision: number; readonly ready: boolean; readonly status: string };
  compareAndSet(revision: number, value: JsonObject): number | undefined;
  flush(): Promise<void>;
  focused(): ReadonlySet<string>;
}

interface ReceiptRecord {
  readonly patch: AssistedDraftPatch;
  readonly receipt: DraftPatchReceipt;
  readonly changes: readonly FieldChange[];
  readonly stage?: "prepared" | "committed";
}

interface AssistanceRuntime {
  readonly activeKey: string | null;
  readonly receipts: ReadonlyMap<string, readonly ReceiptRecord[]>;
  register(binding: LiveDraft): () => void;
  changed(key: string): void;
  reset(key: string): void;
  open(key: string): void;
}

const RuntimeContext = createContext<AssistanceRuntime | null>(null);
const newId = () => globalThis.crypto.randomUUID();
const failure = (error: unknown) => error instanceof Error ? error.message : "Assistance is unavailable. Your form remains editable.";

export interface UseAssistedDraftOptions {
  readonly title?: string;
  readonly interactionMode: "operate" | "arrange" | "preview";
  readonly readOnly?: boolean;
  readonly onOpen?: () => void;
}

export interface AssistedDraftControl {
  readonly active: boolean;
  readonly declared: boolean;
  readonly available: boolean;
  open(): void;
  fieldProps(path: readonly string[]): {
    readonly onFocus: () => void;
    readonly onBlur: () => void;
    readonly "data-assisted-state"?: "applied" | "pending";
    readonly "aria-description"?: string;
  };
}

/** Widget-facing seam: no provider, URL, event stream, or model authority. */
export function useAssistedDraft<Value>(draftName: string, draft: WidgetDraftHandle<Value>, options: UseAssistedDraftOptions): AssistedDraftControl {
  const runtime = useContext(RuntimeContext);
  const declaration = useWidgetAssistanceDeclaration(draftName);
  const form = declaration ? assistedForms[draftName] : undefined;
  const identityKey = widgetDraftStorageKey(draft.identity);
  const draftRef = useRef(draft);
  const optionsRef = useRef(options);
  const mountedRef = useRef(false);
  const focusedRef = useRef(new Set<string>());
  const generationRef = useRef(0);
  draftRef.current = draft;
  optionsRef.current = options;
  const binding = useMemo<LiveDraft | null>(() => {
    if (!declaration || !form || declaration.submitPolicy !== "user_only" || !equalJson(declaration.schema, draft.schema)) return null;
    return {
      key: identityKey, identity: draft.identity, schema: draft.schema, form,
      title: () => optionsRef.current.title ?? form.title,
      editable: () => optionsRef.current.interactionMode === "operate" && !optionsRef.current.readOnly,
      mounted: () => mountedRef.current && widgetDraftStorageKey(draftRef.current.identity) === identityKey,
      generation: () => generationRef.current,
      snapshot: () => draftRef.current.getSnapshot() as unknown as ReturnType<LiveDraft["snapshot"]>,
      compareAndSet: (revision, value) => draftRef.current.compareAndSet(revision, value as Value),
      flush: () => draftRef.current.flush(),
      focused: () => focusedRef.current,
    };
  }, [declaration, form, identityKey]);
  const register = runtime?.register;
  useEffect(() => {
    mountedRef.current = true;
    const cleanup = binding && register ? register(binding) : undefined;
    return () => { mountedRef.current = false; focusedRef.current.clear(); cleanup?.(); };
  }, [binding, register]);
  const reset = runtime?.reset;
  useEffect(() => draft.subscribeReset(() => {
    generationRef.current += 1;
    focusedRef.current.clear();
    reset?.(identityKey);
  }), [draft.subscribeReset, identityKey, reset]);
  const changed = runtime?.changed;
  useEffect(() => { changed?.(identityKey); }, [changed, identityKey, options.interactionMode, options.readOnly, draft.ready]);
  const available = !!runtime && !!binding && options.interactionMode === "operate" && !options.readOnly && draft.ready;
  const records = runtime?.receipts.get(identityKey) ?? [];
  return {
    active: runtime?.activeKey === identityKey,
    declared: !!declaration,
    available,
    open: () => { if (available) { optionsRef.current.onOpen?.(); runtime.open(identityKey); } },
    fieldProps: (path) => {
      const key = pathKey(path);
      const pending = records.some((record) => record.receipt.status !== "rejected" && record.receipt.status !== "undone" && record.receipt.pendingFields.some((field) => pathKey(field.path) === key));
      const applied = records.some((record) => record.receipt.status !== "undone" && record.receipt.status !== "rejected" && record.receipt.appliedFields.some((field) => pathKey(field) === key) && record.patch.operations.some((operation) => pathKey(operation.path) === key && equalJson(readField(draft.value, path), operation.op === "set" ? operation.value : form?.fields.find((field) => pathKey(field.path) === key)?.default)));
      return {
        onFocus: () => { focusedRef.current.add(key); },
        onBlur: () => { focusedRef.current.delete(key); },
        "data-assisted-state": pending ? "pending" : applied ? "applied" : undefined,
        "aria-description": pending ? "Assistant suggestion awaiting review in the assistance panel." : applied ? "Filled by assistant. You can edit or undo this change." : undefined,
      };
    },
  };
}

export function AssistDraftButton({ assistance, children = "Help me shape this" }: { readonly assistance: AssistedDraftControl; readonly children?: ReactNode }) {
  if (!assistance.declared) return null;
  return <Button type="button" disabled={!assistance.available} onClick={assistance.open} aria-expanded={assistance.active}>{children}</Button>;
}

export function AssistedDraftRuntimeProvider({ children, fetchImpl }: { readonly children: ReactNode; readonly fetchImpl?: typeof fetch }) {
  const registry = useRef(new Map<string, LiveDraft>());
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [receipts, setReceipts] = useState<ReadonlyMap<string, readonly ReceiptRecord[]>>(new Map());
  const [, rerender] = useState(0);
  const register = useCallback((binding: LiveDraft) => {
    registry.current.set(binding.key, binding);
    return () => {
      if (registry.current.get(binding.key) === binding) {
        registry.current.delete(binding.key);
        setActiveKey((active) => active === binding.key ? null : active);
      }
    };
  }, []);
  const changed = useCallback(() => rerender((version) => version + 1), []);
  const reset = useCallback((key: string) => {
    try { sessionStorage.removeItem(`wb.assistance.binding:${key}`); } catch { /* optional recovery store */ }
    setActiveKey((current) => current === key ? null : current);
    setReceipts((current) => { const next = new Map(current); next.delete(key); return next; });
  }, []);
  const open = useCallback((key: string) => {
    const binding = registry.current.get(key);
    if (binding?.editable() && binding.mounted()) setActiveKey(key);
  }, []);
  const onRecords = useCallback((key: string, records: readonly ReceiptRecord[]) => setReceipts((current) => new Map(current).set(key, records)), []);
  const value = useMemo(() => ({ activeKey, receipts, register, changed, reset, open }), [activeKey, receipts, register, changed, reset, open]);
  const binding = activeKey ? registry.current.get(activeKey) : undefined;
  return <RuntimeContext.Provider value={value}>
    <div className="wb-assistance-host" data-assistance-open={binding ? "true" : undefined}>
      <div className="wb-assistance-host__content">{children}</div>
      {binding && <AssistantDock key={binding.key} binding={binding} initialRecords={receipts.get(binding.key) ?? []} fetchImpl={fetchImpl} onClose={() => setActiveKey(null)} onEndSession={() => reset(binding.key)} onRecords={onRecords} />}
    </div>
  </RuntimeContext.Provider>;
}

function AssistantDock({ binding, onClose, onEndSession, onRecords, fetchImpl, initialRecords }: {
  readonly binding: LiveDraft;
  readonly onClose: () => void;
  readonly onEndSession: () => void;
  readonly onRecords: (key: string, records: readonly ReceiptRecord[]) => void;
  readonly fetchImpl?: typeof fetch;
  readonly initialRecords: readonly ReceiptRecord[];
}) {
  const fetcher = useMemo(() => fetchImpl ?? globalThis.fetch.bind(globalThis), [fetchImpl]);
  const [availability, setAvailability] = useState<AssistanceAvailability | null>(null);
  const [session, setSession] = useState<AssistanceSession | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [records, setRecords] = useState<readonly ReceiptRecord[]>([]);
  const recordsRef = useRef(new Map<string, ReceiptRecord>());
  const initialRecordsRef = useRef(initialRecords);
  const preparedRef = useRef(new Map<string, PreparedDraftSnapshot>());
  const requestId = useRef(newId());
  const alive = useRef(true);
  const generation = useRef(binding.generation());
  const polling = useRef(false);
  const operationChain = useRef<Promise<void>>(Promise.resolve());
  const storageKey = `wb.assistance.binding:${binding.key}`;
  const receiptStorageKey = (sessionId: string) => `wb.assistance.receipts:${binding.key}:${sessionId}`;
  const editable = binding.editable();
  const canMutate = () => alive.current && binding.mounted() && binding.editable() && generation.current === binding.generation();

  const read = useCallback(async (path: string) => {
    const response = await fetcher(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Assistance could not load.");
    return payload;
  }, [fetcher]);
  const headers = useCallback((operation: string, subject: string, path: string, body: Record<string, unknown>) => exactHumanAuthorityHeaders({ action: `dashboard.assistance.${operation}`, subject, context: { method: "POST", path, body } }, fetcher), [fetcher]);
  const post = useCallback(async (operation: string, subject: string, path: string, body: Record<string, unknown>) => {
    const authority = await headers(operation, subject, path, body);
    const response = await fetcher(path, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "application/json", ...authority }, body: JSON.stringify(body) });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Assistance could not complete this action.");
    return payload;
  }, [fetcher, headers]);
  const acceptSession = useCallback(async (candidate: AssistanceSession) => {
    if (!equalJson(candidate.identity, binding.identity) || !equalJson(candidate.schema, binding.schema) || typeof candidate.assistantSessionId !== "string" || typeof candidate.conversationId !== "string") throw new Error("Assistance returned a different draft binding.");
    let retained: readonly ReceiptRecord[] = initialRecordsRef.current.filter((record) => record.patch.assistantSessionId === candidate.assistantSessionId);
    try {
      const stored = sessionStorage.getItem(receiptStorageKey(candidate.assistantSessionId));
      if (stored) retained = JSON.parse(stored) as readonly ReceiptRecord[];
    } catch { /* the host's in-memory journal still survives panel close */ }
    const recovered: ReceiptRecord[] = [];
    for (const record of retained) {
      try {
        const patch = await validatePatch(record.patch, { identity: binding.identity, assistantSessionId: candidate.assistantSessionId, conversationId: candidate.conversationId, form: binding.form });
        if (record.receipt.patchId !== patch.patchId || !Number.isSafeInteger(record.receipt.resultingRevision) || !["applied", "pending", "partial", "rejected", "undone"].includes(record.receipt.status)) continue;
        for (const change of record.changes) {
          const field = fieldFor(binding.form, change.path);
          validateFieldValue(field, change.after);
          if (change.before !== undefined) validateFieldValue(field, change.before);
          const operation = patch.operations.find((item) => equalJson(item.path, change.path));
          if (!operation || !equalJson(change.after, operation.op === "set" ? operation.value : field.default)) throw new Error("Invalid retained inverse");
        }
        let receipt = record.receipt;
        if (record.stage === "prepared") {
          // The tab may have closed between a draft CAS and its receipt write.
          // Inspect only: never replay an uncertain mutation on recovery.
          const current = binding.snapshot();
          const applied = record.changes.filter((change) => equalJson(readField(current.value, change.path), receipt.status === "undone" ? change.before : change.after));
          const pending = record.changes.filter((change) => !applied.includes(change));
          receipt = { ...receipt, status: receipt.status === "undone" ? "undone" : pending.length ? (applied.length ? "partial" : "pending") : "applied", appliedFields: applied.map((change) => change.path), pendingFields: [...receipt.pendingFields, ...pending.map((change) => ({ path: change.path, reason: "storage_conflict" as const }))], resultingRevision: current.revision, message: "Recovered an interrupted assistant change. Review the field receipts; no fields were changed during recovery." };
        }
        recovered.push({ ...record, patch, receipt, stage: "committed" });
      } catch { /* corrupt or foreign receipt data never grants patch authority */ }
    }
    if (!alive.current) return;
    recordsRef.current = new Map(recovered.map((record) => [record.patch.patchId, record]));
    setRecords(recovered);
    onRecords(binding.key, recovered);
    setSession(candidate);
    setAvailability(candidate.availability);
    try { sessionStorage.setItem(storageKey, candidate.assistantSessionId); } catch { /* session recovery is optional */ }
  }, [binding, onRecords, storageKey]);

  useEffect(() => {
    alive.current = true;
    void read("/api/assistance/availability").then((value: AssistanceAvailability) => { if (alive.current) setAvailability(value); }).catch((reason) => { if (alive.current) setError(failure(reason)); });
    let saved: string | null = null;
    try { saved = sessionStorage.getItem(storageKey); } catch { /* private browsing */ }
    if (saved) void read(`/api/assistance/${encodeURIComponent(saved)}`).then(acceptSession).catch(() => { try { sessionStorage.removeItem(storageKey); sessionStorage.removeItem(receiptStorageKey(saved!)); } catch { /* no storage */ } });
    return () => { alive.current = false; };
  }, [acceptSession, read, storageKey]);

  const retain = (record: ReceiptRecord) => {
    recordsRef.current.set(record.patch.patchId, record);
    const next = [...recordsRef.current.values()];
    try { sessionStorage.setItem(receiptStorageKey(record.patch.assistantSessionId), JSON.stringify(next)); }
    catch { if (alive.current) setError("Local receipt recovery is unavailable after reload. Your form remains editable, and Undo remains available in this open panel."); }
    return next;
  };
  const publish = (record: ReceiptRecord) => {
    const next = retain({ ...record, stage: "committed" });
    setRecords(next);
    onRecords(binding.key, next);
  };
  const acknowledge = useCallback(async (receipt: DraftPatchReceipt) => {
    if (!session) return;
    await post("acknowledge", `assistance:${session.assistantSessionId}`, `/api/assistance/${encodeURIComponent(session.assistantSessionId)}/receipts`, receipt as unknown as Record<string, unknown>);
  }, [post, session]);

  const commitPlan = async (patch: AssistedDraftPatch, plan: PatchPlan, priorChanges: readonly FieldChange[] = []) => {
    if (!canMutate()) return;
    let receipt = plan.receipt;
    let changes = [...priorChanges, ...plan.changes];
    if (plan.changes.length) {
      // Write an inverse/evidence journal before changing the draft. It is not
      // another current draft; recovery compares fields without replaying CAS.
      retain({ patch, receipt, changes, stage: "prepared" });
      const baseRevision = plan.receipt.resultingRevision - 1;
      const revision = binding.compareAndSet(baseRevision, plan.value);
      if (revision === undefined) {
        receipt = { ...receipt, status: "pending", appliedFields: [], pendingFields: patch.operations.map((operation) => ({ path: operation.path, reason: "storage_conflict" as const })), resultingRevision: binding.snapshot().revision, message: "The draft changed before this patch could apply. Review the suggestions." };
        changes = [...priorChanges];
      } else {
        try { await binding.flush(); receipt = { ...receipt, resultingRevision: revision }; }
        catch {
          receipt = { ...receipt, status: "pending", pendingFields: patch.operations.map((operation) => ({ path: operation.path, reason: "storage_conflict" as const })), appliedFields: [], resultingRevision: revision, message: "Assistant edits are visible locally, but storage conflicted. Resolve draft recovery before applying more suggestions." };
        }
      }
    }
    // Clearing the form may finish while its earlier patch save is awaiting
    // storage. Never resurrect receipts in the new editing lifetime either.
    if (generation.current !== binding.generation()) return;
    publish({ patch, receipt, changes });
    try { await acknowledge(receipt); }
    catch (reason) { if (alive.current) setError(`Changes are visible in your form; their receipt will retry. ${failure(reason)}`); }
  };

  const applyPatch = async (unknownPatch: unknown, serverReceipt: DraftPatchReceipt | null) => {
    if (!session || !canMutate()) return;
    const patch = await validatePatch(unknownPatch, { identity: binding.identity, assistantSessionId: session.assistantSessionId, conversationId: session.conversationId, form: binding.form });
    if (!canMutate()) return;
    const local = recordsRef.current.get(patch.patchId);
    if (local) {
      if (serverReceipt && (serverReceipt.resultingRevision > local.receipt.resultingRevision || serverReceipt.status === "undone" || serverReceipt.status === "rejected")) {
        if (!equalJson(local.receipt, serverReceipt)) publish({ ...local, receipt: serverReceipt, changes: [] });
        return;
      }
      if (!serverReceipt || !equalJson(local.receipt, serverReceipt)) await acknowledge(local.receipt);
      return;
    }
    if (serverReceipt) {
      // Receipted patches are never applied a second time after remount/reload.
      publish({ patch, receipt: serverReceipt, changes: [] });
      return;
    }
    await binding.flush();
    if (!canMutate()) return;
    const current = binding.snapshot();
    const plan = planPatch(patch, binding.form, current, binding.focused());
    await commitPlan(patch, plan);
  };

  const pollRef = useRef<() => Promise<void>>(async () => undefined);
  pollRef.current = async () => {
    if (!session || !canMutate() || polling.current) return;
    polling.current = true;
    try {
      const payload = await read(`/api/assistance/${encodeURIComponent(session.assistantSessionId)}/patches`);
      for (const entry of payload.patches ?? []) {
        operationChain.current = operationChain.current.catch(() => undefined).then(() => applyPatch(entry.patch, entry.receipt));
        await operationChain.current;
      }
    } catch (reason) { if (alive.current) setError(failure(reason)); }
    finally { polling.current = false; }
  };
  const poll = useCallback(() => { void pollRef.current(); }, []);
  useEffect(() => {
    if (!session || !editable) return;
    poll();
    const timer = setInterval(poll, 3000);
    return () => clearInterval(timer);
  }, [editable, poll, session]);

  const provider = useMemo(() => session ? new HttpChatConversationProvider({
    conversationId: session.conversationId, fetchImpl: fetcher,
    basePath: `/api/assistance/${encodeURIComponent(session.assistantSessionId)}/conversations`,
    authorizeSend: (body) => {
      if (!binding.mounted() || !binding.editable()) throw new Error("Assistance is paused outside Operate mode.");
      return headers("respond", `assistance:${session.assistantSessionId}`, `/api/assistance/${encodeURIComponent(session.assistantSessionId)}/conversations/${encodeURIComponent(session.conversationId)}/respond`, body);
    },
  }) : null, [binding, fetcher, headers, session]);
  const composerStorageKey = session ? `wb.assistance.composer:${binding.key}:${session.assistantSessionId}` : null;
  const retainedComposer = (): string => {
    try { return composerStorageKey ? sessionStorage.getItem(composerStorageKey) ?? "" : ""; } catch { return ""; }
  };

  const prepareSend = async (input: ChatSendInput): Promise<ChatSendInput> => {
    if (!session || !canMutate() || !input.messageId) throw new Error("Assistance is paused. Your form is unchanged.");
    let prepared = preparedRef.current.get(input.messageId);
    if (!prepared) {
      // Send authorizes the fields visible now, not edits made while their
      // persistence is pending. Freeze values/revision before the first await.
      const current = binding.snapshot();
      const snapshot = discloseSnapshot(binding.form, current.value);
      await binding.flush();
      if (!canMutate()) throw new Error("This draft is no longer active.");
      prepared = { messageId: input.messageId, baseDraftRevision: current.revision, baseSnapshotHash: await snapshotHash(snapshot), snapshot };
      preparedRef.current.set(input.messageId, prepared);
    }
    await post("prepare", `assistance:${session.assistantSessionId}`, `/api/assistance/${encodeURIComponent(session.assistantSessionId)}/snapshots`, prepared as unknown as Record<string, unknown>);
    return input;
  };

  const review = (record: ReceiptRecord, action: "apply" | "reject" | "undo") => {
    operationChain.current = operationChain.current.catch(() => undefined).then(async () => {
      if (!canMutate()) return;
      await binding.flush();
      if (!canMutate()) return;
      const current = binding.snapshot();
      if (action === "reject") {
        const receipt: DraftPatchReceipt = { ...record.receipt, status: record.receipt.appliedFields.length ? "applied" : "rejected", pendingFields: [], resultingRevision: current.revision, message: "Assistant suggestions dismissed. Your form was not changed." };
        publish({ ...record, receipt });
        await acknowledge(receipt);
      } else if (action === "undo") {
        await commitPlan(record.patch, planUndo(record.patch.patchId, record.changes, current, binding.focused()));
      } else {
        const paths = new Set(record.receipt.pendingFields.map((field) => pathKey(field.path)));
        const plan = planPatch(record.patch, binding.form, current, binding.focused(), paths);
        await commitPlan(record.patch, { ...plan, receipt: { ...plan.receipt, appliedFields: [...record.receipt.appliedFields, ...plan.receipt.appliedFields] } }, record.changes);
      }
    }).catch((reason) => { if (alive.current) setError(failure(reason)); });
  };

  const appendix = <div className="wb-assistance-receipts" aria-label="Assistant field changes">
    <p role="status" aria-live="polite">{records[records.length - 1]?.receipt.message}</p>
    {records.map((record) => <section key={record.patch.patchId} className="wb-assistance-receipt" aria-label="Assistant patch receipt">
      <p>{record.receipt.message}</p>
      {record.receipt.appliedFields.length > 0 && <p>Assistant-filled: {record.receipt.appliedFields.map((path) => path.join(" / ")).join(", ")}</p>}
      {record.receipt.pendingFields.length > 0 && <ul>{record.receipt.pendingFields.map((field) => {
        const operation = record.patch.operations.find((candidate) => equalJson(candidate.path, field.path));
        return <li key={pathKey(field.path)}><strong>{field.path.join(" / ")}</strong> — {field.reason.replace(/_/g, " ")}: <span>{operation?.op === "set" ? String(operation.value).slice(0, 300) : "Clear optional field"}</span></li>;
      })}</ul>}
      <div className="wb-assistance-actions">
        {record.receipt.pendingFields.length > 0 && record.receipt.status !== "undone" && <>
          <Button type="button" disabled={!editable} onClick={() => review(record, "apply")}>Apply suggestions</Button>
          <Button type="button" disabled={!editable} onClick={() => review(record, "reject")}>Dismiss suggestions</Button>
        </>}
        {record.changes.length > 0 && record.receipt.status !== "undone" && <Button type="button" disabled={!editable} onClick={() => review(record, "undo")}>Undo assistant changes</Button>}
      </div>
    </section>)}
  </div>;

  return <aside className="wb-assistance-dock" aria-label="Draft assistance">
    <header><h2>{binding.title()}</h2><Button type="button" onClick={onClose}>Close assistance</Button></header>
    <p>Bound to this form. Only you can submit it.</p>
    {!editable && <p role="status">Assistance is paused in read-only, Arrange, or Preview mode. Your draft is unchanged.</p>}
    {error && <p role="alert">{error}</p>}
    {!session && <>
      <p>{availability?.message ?? "Checking assistance availability…"}</p>
      {availability?.disclosure && <p>{availability.disclosure}</p>}
      {availability?.available ? <Button type="button" disabled={busy || !editable} onClick={() => {
        if (!canMutate()) return;
        setBusy(true); setError(null);
        void post("start", `assistance:new:${requestId.current}`, "/api/assistance/sessions", { requestId: requestId.current, identity: binding.identity, schema: binding.schema, interactionMode: "operate", readOnly: false, disclosureAccepted: true, providerId: availability.providerId, modelId: availability.modelId }).then(acceptSession).catch((reason) => { if (alive.current) setError(failure(reason)); }).finally(() => { if (alive.current) setBusy(false); });
      }}>{busy ? "Starting…" : "Start assistance"}</Button> : <a href="/app/settings/apps/dashboard?setting=wb.dashboard.assistance">Set up form assistance</a>}
      <Button type="button" onClick={() => { setError(null); void read("/api/assistance/availability").then(setAvailability).catch((reason) => setError(failure(reason))); }}>Retry availability</Button>
    </>}
    {session && provider && <>
      <p>{session.availability.providerId} · {session.availability.modelId} · Draft shaping only</p>
      <ConversationChat provider={provider} conversationId={session.conversationId} title="Draft conversation" prepareSend={prepareSend} onMessagesChange={poll} transcriptAppendix={appendix} composerDisabled={!editable} responsesDisabled={!editable} readOnlyReason="Assistance is paused outside editable Operate mode." initialValue={retainedComposer()} onDraftChange={(value) => {
        try { if (composerStorageKey) sessionStorage.setItem(composerStorageKey, value); } catch { /* shared composer still owns the live draft */ }
      }} />
      <Button type="button" disabled={!editable} onClick={() => { void post("stop", `assistance:${session.assistantSessionId}`, `/api/assistance/${encodeURIComponent(session.assistantSessionId)}/stop`, {}).catch((reason) => setError(failure(reason))); }}>Stop assistant</Button>
      <Button type="button" disabled={!editable} onClick={() => {
        // Detach synchronously even when a stop acknowledgement is uncertain.
        // Reopening Help will offer a new, explicitly disclosed session.
        alive.current = false;
        void post("stop", `assistance:${session.assistantSessionId}`, `/api/assistance/${encodeURIComponent(session.assistantSessionId)}/stop`, {}).catch(() => undefined);
        onEndSession();
      }}>End session and keep draft</Button>
      <p>Send another message to resume. Closing this panel keeps your draft and conversation.</p>
    </>}
  </aside>;
}
