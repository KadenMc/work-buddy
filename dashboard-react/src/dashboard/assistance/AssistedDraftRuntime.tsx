import { Sparkle } from "@phosphor-icons/react/Sparkle";
import { X } from "@phosphor-icons/react/X";
import { createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type CSSProperties, type ReactNode } from "react";

import type { JsonObject, JsonSchemaReference } from "../contributions/contracts";
import { useWidgetAssistanceDeclaration, type WidgetDraftHandle } from "../drafts/WidgetDraftRuntime";
import { widgetDraftStorageKey, type WidgetDraftIdentity } from "../drafts/contracts";
import { HttpChatConversationProvider } from "../conversations/HttpChatConversationProvider";
import { HttpChatExecutionProfileProvider } from "../conversations/HttpChatExecutionProfileProvider";
import { HelpTarget, type HelpContent } from "../help";
import { ChatCopyAction, ChatPanelState, ConversationChat, useChatExecutionProfile, type ChatConversationProvider, type ChatExecutionControl, type ChatMessage, type ChatSendInput } from "../../widget-library/chat";
import { Button, IconButton, InlineAlert, SegmentedControl, VisuallyHidden } from "../../ui";
import { WorkspaceSidePanel } from "../layout/WorkspaceSidePanel";
import type { AssistanceAvailability, AssistanceSession, AssistanceStartRequest, AssistanceStopRequest, AssistedDraftPatch, AssistedFormSchema, DraftPatchReceipt, PreparedDraftSnapshot } from "./contracts";
import { assistedForms, discloseSnapshot, equalJson, fieldFor, isRecord, pathKey, readField, snapshotHash, validateFieldValue } from "./schema";
import { planPatch, planUndo, validatePatch, type FieldChange, type PatchPlan } from "./patches";
import { AssistanceClient, AssistanceRequestError, assistanceAgent, normalizeAssistanceSession } from "./AssistanceClient";
import { AssistancePauses, AssistanceRevocations, assistanceBindingKey, assistanceComposerKey, assistancePreparationKey, readSessionValue, removeSessionValue, writeSessionValue } from "./recovery";
import "./assistance.css";

interface LiveDraft {
  readonly key: string;
  readonly paneId: string;
  readonly outletId: string;
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
  open(key: string, trigger?: HTMLElement | null): void;
}

const RuntimeContext = createContext<AssistanceRuntime | null>(null);
const WorkspaceOutletContext = createContext<string | null>(null);
const DockContext = createContext<{ readonly outletId: string; readonly open: boolean; readonly opener: HTMLElement | null; readonly content: ReactNode } | null>(null);
const newId = () => globalThis.crypto.randomUUID();
const failure = (error: unknown) => error instanceof Error ? error.message : "Assistance is unavailable. Your form remains editable.";
const DRAFT_ASSISTANCE_HELP: HelpContent = {
  summary: "Shape this form with an assistant.",
  details: "The assistant fills these fields. You can edit or undo its suggestions; only you can submit the form.",
};
const AVAILABILITY_LABELS: Readonly<Record<AssistanceAvailability["code"], string>> = {
  ready: "Ready to launch",
  not_configured: "Form assistance is not configured.",
  disabled: "Form assistance is off.",
  invalid_configuration: "Form assistance needs attention.",
  unsupported_provider: "This model does not support form assistance.",
  provider_unavailable: "Assistance availability could not be checked.",
};

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
  readonly panelId: string;
  open(trigger?: HTMLElement | null): void;
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
  const outletId = useContext(WorkspaceOutletContext);
  const declaration = useWidgetAssistanceDeclaration(draftName);
  const form = declaration ? assistedForms[draftName] : undefined;
  const identityKey = widgetDraftStorageKey(draft.identity);
  const paneId = `wb-assisted-draft-${useId()}`;
  const draftRef = useRef(draft);
  const optionsRef = useRef(options);
  const mountedRef = useRef(false);
  const focusedRef = useRef(new Set<string>());
  const generationRef = useRef(0);
  draftRef.current = draft;
  optionsRef.current = options;
  const binding = useMemo<LiveDraft | null>(() => {
    if (!outletId || !declaration || !form || declaration.submitPolicy !== "user_only" || !equalJson(declaration.schema, draft.schema)) return null;
    return {
      key: identityKey, paneId, outletId, identity: draft.identity, schema: draft.schema, form,
      title: () => optionsRef.current.title ?? form.title,
      editable: () => optionsRef.current.interactionMode === "operate" && !optionsRef.current.readOnly,
      mounted: () => mountedRef.current && widgetDraftStorageKey(draftRef.current.identity) === identityKey,
      generation: () => generationRef.current,
      snapshot: () => draftRef.current.getSnapshot() as unknown as ReturnType<LiveDraft["snapshot"]>,
      compareAndSet: (revision, value) => draftRef.current.compareAndSet(revision, value as Value),
      flush: () => draftRef.current.flush(),
      focused: () => focusedRef.current,
    };
  }, [declaration, form, identityKey, outletId, paneId]);
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
    panelId: paneId,
    open: (trigger) => { if (available) { optionsRef.current.onOpen?.(); runtime.open(identityKey, trigger); } },
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

export function AssistDraftButton({ assistance, children = "AI help" }: { readonly assistance: AssistedDraftControl; readonly children?: ReactNode }) {
  const trigger = useRef<HTMLButtonElement>(null);
  if (!assistance.declared) return null;
  return <HelpTarget content={DRAFT_ASSISTANCE_HELP} reactAriaComposite><Button ref={trigger} type="button" disabled={!assistance.available} onClick={() => assistance.open(trigger.current)} aria-expanded={assistance.active} aria-controls={assistance.panelId}><Sparkle weight="duotone" aria-hidden="true" />{children}</Button></HelpTarget>;
}

export function AssistedDraftRuntimeProvider({ children, fetchImpl }: { readonly children: ReactNode; readonly fetchImpl?: typeof fetch }) {
  const registry = useRef(new Map<string, LiveDraft>());
  const sessionsByDraft = useRef(new Map<string, string>());
  const client = useMemo(() => new AssistanceClient(fetchImpl), [fetchImpl]);
  const revocations = useMemo(() => new AssistanceRevocations(client), [client]);
  const pauses = useMemo(() => new AssistancePauses(client), [client]);
  useSyncExternalStore(revocations.subscribe, revocations.getSnapshot);
  useSyncExternalStore(pauses.subscribe, pauses.getSnapshot);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [displayedKey, setDisplayedKey] = useState<string | null>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const closingFocus = useRef<HTMLElement | null>(null);
  const [receipts, setReceipts] = useState<ReadonlyMap<string, readonly ReceiptRecord[]>>(new Map());
  const [, rerender] = useState(0);
  const register = useCallback((binding: LiveDraft) => {
    registry.current.set(binding.key, binding);
    return () => {
      if (registry.current.get(binding.key) === binding) {
        registry.current.delete(binding.key);
        setActiveKey((active) => active === binding.key ? null : active);
        setDisplayedKey((current) => current === binding.key ? null : current);
      }
    };
  }, []);
  const changed = useCallback(() => rerender((version) => version + 1), []);
  const reset = useCallback((key: string) => {
    const sessionId = sessionsByDraft.current.get(key) ?? readSessionValue(assistanceBindingKey(key));
    // The host reset has already fenced its editing generation. Tombstone
    // the old session before forgetting any identity or awaiting cancellation.
    if (sessionId) revocations.revoke(key, sessionId);
    sessionsByDraft.current.delete(key);
    removeSessionValue(assistanceBindingKey(key));
    removeSessionValue(assistancePreparationKey(key));
    removeSessionValue(`wb.assistance.journal:${key}`);
    removeSessionValue(`wb.assistance.history:${key}`);
    setActiveKey((current) => current === key ? null : current);
    setDisplayedKey((current) => current === key ? null : current);
    setReceipts((current) => { const next = new Map(current); next.delete(key); return next; });
  }, [revocations]);
  const open = useCallback((key: string, trigger?: HTMLElement | null) => {
    const binding = registry.current.get(key);
    if (binding?.editable() && binding.mounted()) {
      returnFocus.current = trigger ?? (document.activeElement instanceof HTMLElement && document.activeElement !== document.body ? document.activeElement : null);
      setDisplayedKey(key);
      setActiveKey(key);
    }
  }, []);
  const close = useCallback(() => {
    closingFocus.current = returnFocus.current;
    setActiveKey(null);
  }, []);
  useLayoutEffect(() => {
    if (activeKey !== null) return;
    const trigger = closingFocus.current;
    closingFocus.current = null;
    // The compact primary panel must be unhidden before it can accept focus.
    if (trigger?.isConnected) trigger.focus({ preventScroll: true });
  }, [activeKey]);
  useEffect(() => {
    const retry = () => { void revocations.retry(); void pauses.retry(); };
    retry();
    window.addEventListener("online", retry);
    window.addEventListener("focus", retry);
    const timer = setInterval(retry, 10000);
    return () => {
      clearInterval(timer);
      window.removeEventListener("online", retry);
      window.removeEventListener("focus", retry);
    };
  }, [pauses, revocations]);
  const onRecords = useCallback((key: string, records: readonly ReceiptRecord[]) => setReceipts((current) => new Map(current).set(key, records)), []);
  const onSession = useCallback((key: string, sessionId: string) => { sessionsByDraft.current.set(key, sessionId); }, []);
  const value = useMemo(() => ({ activeKey, receipts, register, changed, reset, open }), [activeKey, receipts, register, changed, reset, open]);
  const binding = displayedKey ? registry.current.get(displayedKey) : undefined;
  // The root owns cancellation, but the workspace outlet must render the dock:
  // its local Help/router context does not reach a sibling rendered above App.
  const dock = binding ? {
    outletId: binding.outletId,
    open: activeKey === binding.key,
    opener: returnFocus.current,
    content: <AssistantDock key={binding.key} binding={binding} open={activeKey === binding.key} initialRecords={receipts.get(binding.key) ?? []} client={client} revocations={revocations} pauses={pauses} onClose={close} onRecords={onRecords} onSession={onSession} />,
  } : null;
  return <RuntimeContext.Provider value={value}>
    {revocations.error && <InlineAlert tone="warning" role="status" className="wb-assistance-cancellation">{revocations.error}<Button type="button" size="small" onClick={() => { void revocations.retry(); }}>Retry cancellation</Button></InlineAlert>}
    {pauses.error && <InlineAlert tone="warning" role="status" className="wb-assistance-cancellation">{pauses.error}<Button type="button" size="small" onClick={() => { void pauses.retry(); }}>Retry pending Stop</Button></InlineAlert>}
    <DockContext.Provider value={dock}>{children}</DockContext.Provider>
  </RuntimeContext.Provider>;
}

const pixels = (value: string): number => Number.parseFloat(value) || 0;
const blockEdges = (style: CSSStyleDeclaration): number =>
  pixels(style.paddingTop) + pixels(style.paddingBottom) + pixels(style.borderTopWidth) + pixels(style.borderBottomWidth);

/** Rendered, non-shrinking chrome around one flexible child; never its scroll content. */
function fixedBlockSize(element: HTMLElement, flexible?: HTMLElement): number {
  const style = getComputedStyle(element);
  const children = Array.from(element.children).filter((child): child is HTMLElement => child instanceof HTMLElement)
    .map((child) => ({ child, style: getComputedStyle(child) }))
    .filter(({ style: childStyle }) => childStyle.display !== "none" && childStyle.position !== "absolute" && childStyle.position !== "fixed");
  return blockEdges(style) + pixels(style.rowGap) * Math.max(0, children.length - 1)
    + children.reduce((height, { child, style: childStyle }) => height + pixels(childStyle.marginTop) + pixels(childStyle.marginBottom)
      + (child === flexible ? 0 : child.getBoundingClientRect().height), 0);
}

function assistanceContentMinimum(element: HTMLElement): number | null {
  const dock = element.querySelector<HTMLElement>(".wb-assistance-dock");
  if (!dock || dock.closest("[hidden]") || dock.getBoundingClientRect().height <= 0) return null;
  const body = dock.querySelector<HTMLElement>(":scope > .wb-assistance-dock__body");
  const chat = body?.querySelector<HTMLElement>(":scope > .wb-chat-panel");
  if (!body || !chat) return null;
  const dockMinimum = fixedBlockSize(dock, body) + fixedBlockSize(body, chat);
  const state = chat.querySelector<HTMLElement>(":scope > .wb-chat-state");
  if (state) {
    // Loading, unavailable and recovery states have no transcript to shrink.
    // Their actual copy, picker and Retry controls must remain page-reachable.
    const stateMinimum = Math.max(pixels(getComputedStyle(state).minHeight), fixedBlockSize(state));
    return Math.ceil(dockMinimum + fixedBlockSize(chat, state) + stateMinimum);
  }
  const transcript = chat?.querySelector<HTMLElement>(":scope > .wb-chat-list");
  const scroll = transcript?.querySelector<HTMLElement>(":scope > .wb-chat-list__scroll");
  if (!transcript || !scroll) return null;
  const textStyle = getComputedStyle(scroll);
  // A short, font-scaled transcript viewport remains scrollable. Only chrome
  // and controls contribute to the rest of the minimum, not message history.
  const lineHeight = pixels(textStyle.lineHeight) || pixels(textStyle.fontSize) * 1.5;
  const transcriptMinimum = fixedBlockSize(transcript, scroll) + blockEdges(textStyle) + lineHeight * 3;
  return Math.ceil(dockMinimum + fixedBlockSize(chat, transcript) + transcriptMinimum);
}

/** A view-local presentation outlet, below page chrome and the existing Help provider. */
export function AssistedDraftWorkspace({ children, viewId }: { readonly children: ReactNode; readonly viewId: string }) {
  const outletId = useId();
  const dock = useContext(DockContext);
  const matches = dock?.outletId === outletId;
  const open = matches && dock.open;
  const bodyRef = useRef<HTMLDivElement>(null);
  const [bounds, setBounds] = useState<{ width: number; height: number; contentMinimum: number } | null>(null);
  const [pane, setPane] = useState<"form" | "assistance">("assistance");
  const paneRef = useRef(pane);
  paneRef.current = pane;
  const wideningFocus = useRef<{ element: HTMLElement; pane: "form" | "assistance" } | null>(null);
  const wasOpen = useRef(false);
  const pendingOpenFocus = useRef<HTMLElement | null>(null);
  const compact = bounds !== null && (bounds.width < 880 || bounds.height < 400);

  useLayoutEffect(() => {
    const element = bodyRef.current;
    if (!element) return;
    let frame = 0;
    const measure = () => {
      const rect = element.getBoundingClientRect();
      if (rect.width <= 0) return;
      // The viewport limit is measured from this view's actual content origin,
      // not from the top of App or an assumed navbar height.
      const viewport = window.visualViewport;
      const viewportBottom = viewport ? viewport.offsetTop + viewport.height : window.innerHeight;
      const next = { width: Math.round(rect.width), height: Math.round(viewportBottom - Math.max(0, rect.top) - 16) };
      const nextCompact = next.width < 880 || next.height < 400;
      if (nextCompact && element.querySelector(".wb-assistance-workspace__form")?.contains(document.activeElement)) setPane("form");
      if (!nextCompact && document.activeElement instanceof HTMLElement && element.parentElement?.querySelector(".wb-segmented-field")?.contains(document.activeElement)) {
        wideningFocus.current = { element: document.activeElement, pane: paneRef.current };
      }
      const measuredMinimum = assistanceContentMinimum(element);
      setBounds((current) => {
        // A compact hidden pane has no geometry. Keep its last valid minimum
        // until it is shown and measured again, without remounting its state.
        const contentMinimum = measuredMinimum ?? current?.contentMinimum ?? 0;
        return current?.width === next.width && current.height === next.height && current.contentMinimum === contentMinimum
          ? current : { ...next, contentMinimum };
      });
    };
    const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(measure); };
    const observer = new ResizeObserver(schedule);
    observer.observe(element);
    if (element.parentElement) observer.observe(element.parentElement);
    const view = element.closest(".wb-view-host");
    if (view) observer.observe(view);
    const contentRoot = element.querySelector<HTMLElement>(".wb-assistance-workspace__panel");
    if (contentRoot) observer.observe(contentRoot);
    const observedContent = new Set<HTMLElement>();
    const observeContent = () => {
      const current = new Set(contentRoot?.querySelectorAll<HTMLElement>(
        ".wb-assistance-dock, .wb-assistance-dock > *, .wb-assistance-dock__body > .wb-chat-panel, .wb-assistance-dock__body > .wb-chat-panel > *, .wb-assistance-dock__body > .wb-chat-panel > .wb-chat-list > :not(.wb-chat-list__scroll), .wb-assistance-dock__body > .wb-chat-panel > .wb-chat-state > *",
      ) ?? []);
      for (const node of observedContent) {
        if (!current.has(node)) { observer.unobserve?.(node); observedContent.delete(node); }
      }
      for (const node of current) {
        if (!observedContent.has(node)) { observer.observe(node); observedContent.add(node); }
      }
    };
    // Loading the canonical chat, expanding details, or changing the composer
    // can change its minimum without resizing the currently clipped host.
    const mutations = new MutationObserver((records) => {
      if (records.every((record) => (record.target instanceof Element ? record.target : record.target.parentElement)?.closest(".wb-chat-list__scroll"))) return;
      observeContent();
      schedule();
    });
    if (contentRoot) mutations.observe(contentRoot, { childList: true, subtree: true, attributes: true, attributeFilter: ["open", "hidden"] });
    observeContent();
    window.addEventListener("resize", schedule);
    window.addEventListener("scroll", schedule, true);
    window.visualViewport?.addEventListener("resize", schedule);
    window.visualViewport?.addEventListener("scroll", schedule);
    measure();
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      mutations.disconnect();
      window.removeEventListener("resize", schedule);
      window.removeEventListener("scroll", schedule, true);
      window.visualViewport?.removeEventListener("resize", schedule);
      window.visualViewport?.removeEventListener("scroll", schedule);
    };
  }, []);

  // Explicitly reopening brings AI help into view. A responsive transition
  // keeps the form visible if that is where the user is currently typing.
  useLayoutEffect(() => {
    if (open && !wasOpen.current) {
      setPane("assistance");
      pendingOpenFocus.current = compact && matches ? dock.opener : null;
    }
    wasOpen.current = open;
    if (!open) pendingOpenFocus.current = null;
  }, [compact, dock, matches, open]);
  useLayoutEffect(() => {
    if (!open || !compact || pane !== "assistance") return;
    const opener = pendingOpenFocus.current;
    pendingOpenFocus.current = null;
    if (opener && (document.activeElement === opener || document.activeElement === document.body)) {
      bodyRef.current?.querySelector<HTMLHeadingElement>(".wb-assistance-dock h2")?.focus({ preventScroll: true });
    }
  }, [compact, open, pane]);
  useLayoutEffect(() => {
    if (compact) return;
    const pending = wideningFocus.current;
    wideningFocus.current = null;
    if (!pending || (document.activeElement !== pending.element && document.activeElement !== document.body)) return;
    const target = pending.pane === "assistance"
      ? bodyRef.current?.querySelector<HTMLElement>(".wb-assistance-dock h2")
      : bodyRef.current?.querySelector<HTMLElement>(".wb-assistance-workspace__form input:not(:disabled), .wb-assistance-workspace__form textarea:not(:disabled), .wb-assistance-workspace__form button:not(:disabled)");
    target?.focus({ preventScroll: true });
  }, [compact]);
  const mode = !open ? "primary-only" : compact ? pane === "form" ? "primary-only" : "side-only" : "split";
  const style = open && bounds ? {
    "--wb-assistance-workspace-height": `${Math.max(384, bounds.height)}px`,
    "--wb-assistance-workspace-content-minimum": `${bounds.contentMinimum}px`,
  } as CSSProperties : undefined;

  return <WorkspaceOutletContext.Provider value={outletId}>
    <div className="wb-assistance-workspace" data-assistance-open={open ? "true" : undefined} data-compact={compact ? "true" : undefined}>
      {open && compact && <SegmentedControl<"form" | "assistance"> label="Workspace pane" value={pane} onChange={setPane} options={[{ value: "form", label: "Form" }, { value: "assistance", label: "AI help" }]} />}
      <div ref={bodyRef} className="wb-assistance-workspace__body" style={style}>
        <WorkspaceSidePanel
          layoutId={`wb.workspace-side-panel:${viewId}`}
          primaryId={`${viewId}:content`} sideId={`${viewId}:side-panel`}
          mode={mode} primary={children} side={matches ? dock.content : null}
          resizeLabel="Resize the AI help side panel"
          sideMinSize="18rem" primaryMinSize="30%"
          primaryClassName="wb-assistance-workspace__form" sideClassName="wb-assistance-workspace__panel"
        />
      </div>
    </div>
  </WorkspaceOutletContext.Provider>;
}

function AssistantDock({ binding, open, onClose, onRecords, onSession, client, revocations, pauses, initialRecords }: {
  readonly binding: LiveDraft;
  readonly open: boolean;
  readonly onClose: () => void;
  readonly onRecords: (key: string, records: readonly ReceiptRecord[]) => void;
  readonly onSession: (key: string, sessionId: string) => void;
  readonly client: AssistanceClient;
  readonly revocations: AssistanceRevocations;
  readonly pauses: AssistancePauses;
  readonly initialRecords: readonly ReceiptRecord[];
}) {
  const fetcher = client.fetcher;
  const pauseRevision = useSyncExternalStore(pauses.subscribe, pauses.getSnapshot);
  const [availability, setAvailability] = useState<AssistanceAvailability | null>(null);
  const [session, setSession] = useState<AssistanceSession | null>(null);
  const sessionRef = useRef<AssistanceSession | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [preparing, setPreparing] = useState(true);
  const paneRef = useRef<HTMLElement>(null);
  const startButton = useRef<HTMLButtonElement>(null);
  const freshStartButton = useRef<HTMLButtonElement>(null);
  const startFocus = useRef<HTMLElement | null>(null);
  const [stopPending, setStopPending] = useState(false);
  const stopPendingRef = useRef(false);
  const [historicalSessions, setHistoricalSessions] = useState<readonly AssistanceSession[]>([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [records, setRecords] = useState<readonly ReceiptRecord[]>([]);
  const [chatTranscript, setChatTranscript] = useState<{
    readonly conversationId: string;
    readonly messages: readonly ChatMessage[];
  } | null>(null);
  const recordsRef = useRef(new Map<string, ReceiptRecord>());
  const initialRecordsRef = useRef(initialRecords);
  const preparedRef = useRef(new Map<string, PreparedDraftSnapshot>());
  const requestId = useRef(readSessionValue(assistancePreparationKey(binding.key)) ?? newId());
  const startAttempt = useRef<AssistanceStartRequest | null>(null);
  const startInFlight = useRef(false);
  const composerTransfer = useRef<string | null>(null);
  const authorityEpoch = useRef(0);
  const alive = useRef(true);
  const generation = useRef(binding.generation());
  const polling = useRef(false);
  const operationChain = useRef<Promise<void>>(Promise.resolve());
  const storageKey = assistanceBindingKey(binding.key);
  const journalKey = `wb.assistance.journal:${binding.key}`;
  const historyKey = `wb.assistance.history:${binding.key}`;
  const receiptStorageKey = (sessionId: string) => `wb.assistance.receipts:${binding.key}:${sessionId}`;
  const startStorageKey = (sessionId: string) => `wb.assistance.start:${binding.key}:${sessionId}`;
  const editable = binding.editable();
  const canMutate = () => alive.current && binding.mounted() && binding.editable() && generation.current === binding.generation();
  const canSend = () => canMutate() && sessionRef.current?.phase === "active" && sessionRef.current.availability.available && !stopPendingRef.current && !pauses.hasPending(sessionRef.current.assistantSessionId) && !revocations.isEnded(sessionRef.current.assistantSessionId);
  const scopedStop = (current: AssistanceSession): AssistanceStopRequest => pauses.requestFor(current.assistantSessionId) ?? {
    requestId: newId(), expected_control_revision: current.controlRevision ?? 0,
    ...(startAttempt.current ? { startRequestId: startAttempt.current.requestId } : {}),
  };

  const read = useCallback((path: string) => client.read(path), [client]);
  const post = useCallback((operation: string, subject: string, path: string, body: Record<string, unknown>, stillCurrent?: () => boolean) => client.post(operation, subject, path, body, stillCurrent), [client]);
  const reconcileStartAttempt = useCallback((candidate: AssistanceSession, restore: boolean) => {
    const key = `wb.assistance.start:${binding.key}:${candidate.assistantSessionId}`;
    if (restore) {
      const saved = readSessionValue(key);
      try { startAttempt.current = saved ? JSON.parse(saved) as AssistanceStartRequest : null; } catch { startAttempt.current = null; }
    }
    const attempt = startAttempt.current;
    // Use the authoritative phase, not our optimistic local Stop projection.
    // A newer non-active control revision proves this frozen attempt cannot
    // launch again. An unchanged revision still permits its exact retry.
    if (attempt && (candidate.phase === "prepared" || candidate.phase === "stopped") && Number.isSafeInteger(candidate.controlRevision) && Number.isSafeInteger(attempt.expected_control_revision) && candidate.controlRevision! > attempt.expected_control_revision) {
      startAttempt.current = null;
      removeSessionValue(key);
    }
  }, [binding.key]);
  const acceptSession = useCallback(async (candidate: AssistanceSession) => {
    if (!equalJson(candidate.identity, binding.identity) || !equalJson(candidate.schema, binding.schema) || typeof candidate.assistantSessionId !== "string" || typeof candidate.conversationId !== "string") throw new Error("Assistance returned a different draft binding.");
    if (sessionRef.current?.assistantSessionId !== candidate.assistantSessionId) {
      stopPendingRef.current = pauses.hasPending(candidate.assistantSessionId);
      setStopPending(stopPendingRef.current);
    }
    const projected = revocations.isEnded(candidate.assistantSessionId) ? { ...candidate, phase: "ended" as const, activeStartId: null }
      : stopPendingRef.current && candidate.phase === "active" ? { ...candidate, phase: "stopped" as const, activeStartId: null } : candidate;
    if (!binding.mounted() || generation.current !== binding.generation()) return;
    onSession(binding.key, candidate.assistantSessionId);
    writeSessionValue(storageKey, candidate.assistantSessionId);
    if (!alive.current) return;
    if (sessionRef.current?.assistantSessionId === candidate.assistantSessionId) {
      // Lifecycle/model refresh must not rebuild providers or erase the inverse journal.
      reconcileStartAttempt(candidate, false);
      sessionRef.current = projected;
      setSession(projected);
      setAvailability(candidate.availability);
      return;
    }
    let retained: readonly ReceiptRecord[] = [...initialRecordsRef.current, ...recordsRef.current.values()];
    try {
      const stored = sessionStorage.getItem(journalKey) ?? sessionStorage.getItem(receiptStorageKey(candidate.assistantSessionId));
      if (stored) retained = JSON.parse(stored) as readonly ReceiptRecord[];
    } catch { /* the host's in-memory journal still survives panel close */ }
    const recovered: ReceiptRecord[] = [];
    for (const record of retained) {
      try {
        const patch = await validatePatch(record.patch, { identity: binding.identity, assistantSessionId: record.patch.assistantSessionId, conversationId: record.patch.conversationId, form: binding.form });
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
    if (!alive.current || generation.current !== binding.generation()) return;
    recordsRef.current = new Map(recovered.map((record) => [record.patch.patchId, record]));
    setRecords(recovered);
    onRecords(binding.key, recovered);
    sessionRef.current = projected;
    if (composerTransfer.current !== null) {
      writeSessionValue(assistanceComposerKey(binding.key, candidate.assistantSessionId), composerTransfer.current);
      composerTransfer.current = null;
    }
    setSession(projected);
    setAvailability(candidate.availability);
    reconcileStartAttempt(candidate, true);
  }, [binding, journalKey, onRecords, onSession, pauses, reconcileStartAttempt, revocations, storageKey]);

  const prepareSession = useCallback(async () => {
    if (!canMutate()) return;
    setPreparing(true);
    setError(null);
    const expectedRequestId = requestId.current;
    writeSessionValue(assistancePreparationKey(binding.key), expectedRequestId);
    try {
      const candidate = normalizeAssistanceSession(await post("prepare_session", `assistance:new:${expectedRequestId}`, "/api/assistance/sessions", {
        requestId: expectedRequestId, identity: binding.identity, schema: binding.schema, interactionMode: "operate", readOnly: false,
      }));
      const storedRequestId = readSessionValue(assistancePreparationKey(binding.key));
      if (generation.current !== binding.generation() || (storedRequestId !== null && storedRequestId !== expectedRequestId)) {
        revocations.revoke(binding.key, candidate.assistantSessionId);
        return;
      }
      await acceptSession(candidate);
    } catch (reason) { if (alive.current) setError(failure(reason)); }
    finally { if (alive.current) setPreparing(false); }
  }, [acceptSession, binding, post, revocations]);

  const load = useCallback(async () => {
    setPreparing(true);
    setError(null);
    try {
      const available = await client.availability();
      if (!alive.current) return;
      setAvailability(available);
      const saved = readSessionValue(storageKey);
      if (saved) {
        // A legacy/ended binding remains inspectable. It is never upgraded or
        // forgotten just because the server requires a newly disclosed Start.
        await acceptSession(await client.session(saved));
      } else if (available.available && canMutate()) {
        await prepareSession();
      }
    } catch (reason) { if (alive.current) setError(failure(reason)); }
    finally { if (alive.current) setPreparing(false); }
  }, [acceptSession, client, prepareSession, storageKey]);

  useEffect(() => {
    alive.current = true;
    try {
      const saved: unknown = JSON.parse(readSessionValue(historyKey) ?? "[]");
      if (Array.isArray(saved)) setHistoricalSessions(saved.map(normalizeAssistanceSession).filter((item) => equalJson(item.identity, binding.identity) && equalJson(item.schema, binding.schema)));
    } catch { /* Historical metadata is not permission to run an assistant. */ }
    void load();
    return () => {
      alive.current = false;
      authorityEpoch.current += 1;
      const current = sessionRef.current;
      // Closing only hides this mounted pane. Actual host detachment fences
      // the current driver; a later visit needs a new explicit Start.
      if (current && !revocations.isEnded(current.assistantSessionId) && (current.phase === "active" || startInFlight.current || startAttempt.current)) {
        // Persist an exact Stop before awaiting it, including a Start whose
        // acknowledgement has not arrived. Server CAS prevents this retry
        // from ever stopping a subsequently authorized generation.
        void pauses.pause(binding.key, current.assistantSessionId, scopedStop(current)).catch(() => undefined);
      }
    };
  }, [binding, client, historyKey, load, pauses, revocations]);

  const sessionId = session?.assistantSessionId ?? null;
  const conversationId = session?.conversationId ?? null;
  const chatMessages = chatTranscript?.conversationId === conversationId
    ? chatTranscript.messages
    : [];
  const executionProvider = useMemo(() => {
    if (sessionId === null || sessionRef.current?.protocol !== "wb.assisted-draft.session/v2" || sessionRef.current.phase === "ended" || sessionRef.current.phase === "expired") return null;
    const path = `/api/assistance/sessions/${encodeURIComponent(sessionId)}/execution`;
    return new HttpChatExecutionProfileProvider({
      targetId: sessionId, loadUrl: path, selectUrl: path, fetchImpl: fetcher,
      initialSnapshot: sessionRef.current.execution,
      authorizeSelect: async (body) => {
        const epoch = authorityEpoch.current;
        if (!canMutate() || revocations.isEnded(sessionId)) throw new Error("Model selection is unavailable for this draft.");
        const headers = await client.authorize("execution_select", `assistance:${sessionId}`, path, body, "PATCH");
        if (!canMutate() || epoch !== authorityEpoch.current || revocations.isEnded(sessionId)) throw new Error("This assistant was paused before its model could change.");
        return headers;
      },
      onEnvelope: (envelope) => {
        const current = sessionRef.current;
        if (!alive.current || current?.assistantSessionId !== sessionId) return;
        const agent = assistanceAgent(envelope.agent);
        const changed = current.execution?.selection.revision !== envelope.execution.selection.revision;
        if (changed) {
          authorityEpoch.current += 1;
          startAttempt.current = null;
          removeSessionValue(startStorageKey(sessionId));
        }
        const next: AssistanceSession = {
          ...current, execution: envelope.execution, agent: agent ?? current.agent,
          phase: revocations.isEnded(sessionId) ? "ended" : agent?.phase ?? current.phase,
          activeStartId: agent?.activeStartId ?? null,
          controlRevision: agent?.controlRevision ?? current.controlRevision,
        };
        sessionRef.current = next;
        setSession(next);
      },
    });
    // Only opaque binding identity owns the transport, never its lifecycle projection.
  }, [binding, client, fetcher, revocations, sessionId]);
  const executionState = useChatExecutionProfile(executionProvider, sessionId ?? "unbound");
  useEffect(() => {
    if (session?.execution) executionProvider?.replaceSnapshot(session.execution);
  }, [executionProvider, session?.execution]);
  const execution: ChatExecutionControl | undefined = executionState === undefined ? undefined : {
    ...executionState,
    confirmSelection: ({ providerLabel, modelLabel }) => sessionRef.current?.phase === "active" || startAttempt.current !== null ? {
      title: `Switch to ${providerLabel} · ${modelLabel}?`,
      description: "This stops the current assistant. Your messages, draft and Undo stay here. Review the disclosure and choose Launch before the new model receives any content.",
      confirmLabel: "Switch model",
    } : null,
    select: async (providerId, modelId) => {
      authorityEpoch.current += 1;
      await executionState.select(providerId, modelId);
    },
  };

  const recoverAfterStop = useCallback(async (current: AssistanceSession, alreadyAbsent = false) => {
    if (!alreadyAbsent) {
      try { await acceptSession(await client.session(current.assistantSessionId)); return; }
      catch (reason) {
        if (!(reason instanceof AssistanceRequestError && reason.status === 404 && reason.code === "assistance_session_not_found")) throw reason;
      }
    }
    if (!alive.current || sessionRef.current?.assistantSessionId !== current.assistantSessionId) return;
    // Confirmed absence is terminal, not an unconfirmed cancellation. Retain
    // the known binding as history and require an explicit new-session action.
    revocations.revoke(binding.key, current.assistantSessionId);
    startAttempt.current = null;
    removeSessionValue(startStorageKey(current.assistantSessionId));
    await acceptSession({ ...current, phase: "ended", activeStartId: null });
    setError("The previous AI help session no longer exists. Your form and locally retained Undo are preserved.");
  }, [acceptSession, binding, client, revocations]);

  const stop = useCallback(async () => {
    const current = sessionRef.current;
    if (!current || current.phase === "ended" || current.phase === "restart_required") return;
    authorityEpoch.current += 1;
    stopPendingRef.current = true;
    setStopPending(true);
    const request = scopedStop(current);
    startAttempt.current = null;
    removeSessionValue(startStorageKey(current.assistantSessionId));
    const paused = { ...current, phase: "stopped" as const, activeStartId: null };
    sessionRef.current = paused;
    setSession(paused);
    setError(null);
    try {
      const result = await pauses.pause(binding.key, current.assistantSessionId, request);
      if (!alive.current || sessionRef.current?.assistantSessionId !== current.assistantSessionId) return;
      stopPendingRef.current = false;
      setStopPending(false);
      await recoverAfterStop(current, result.outcome === "already_absent");
    } catch (reason) {
      if (alive.current) setError(`Stopping is not confirmed yet. Retry Stop before starting again. ${failure(reason)}`);
    }
  }, [binding, pauses, recoverAfterStop]);
  useEffect(() => {
    const current = sessionRef.current;
    if (!current || !stopPendingRef.current || pauses.hasPending(current.assistantSessionId)) return;
    stopPendingRef.current = false;
    setStopPending(false);
    setError((currentError) => currentError?.startsWith("Stopping is not confirmed yet.") ? null : currentError);
    // A reconnect may acknowledge a superseded Stop. Recover server state;
    // never replace a newer active generation with the old local projection.
    void recoverAfterStop(current).catch((reason) => { if (alive.current) setError(failure(reason)); });
  }, [pauseRevision, pauses, recoverAfterStop, sessionId]);
  useEffect(() => {
    if ((!editable || availability?.available === false) && (sessionRef.current?.phase === "active" || startInFlight.current) && !stopPendingRef.current) void stop();
  }, [availability?.available, editable, session?.phase, stop]);

  const start = (fresh = false): void => {
    const currentSession = sessionRef.current;
    const selection = execution?.snapshot?.selection;
    if (!currentSession || !selection || !Number.isSafeInteger(currentSession.controlRevision) || !canMutate() || startInFlight.current || stopPendingRef.current || pauses.hasPending(currentSession.assistantSessionId) || !availability?.available || !execution?.currentAvailable || execution.selecting || revocations.isEnded(currentSession.assistantSessionId) || currentSession.phase === "restart_required") return;
    const expectedEpoch = authorityEpoch.current;
    try {
      const focused = document.activeElement;
      startFocus.current = focused instanceof HTMLElement && (focused === startButton.current || focused === freshStartButton.current) ? focused : null;
      if (fresh) startAttempt.current = null;
      if (startAttempt.current === null) {
        // This is the disclosure boundary: freeze all fields and identities
        // synchronously, before hashing, persistence, authentication or launch.
        const current = binding.snapshot();
        startAttempt.current = {
          requestId: newId(), disclosureAccepted: true,
          provider_id: selection.providerId, model_id: selection.modelId, expected_revision: selection.revision,
          expected_control_revision: currentSession.controlRevision!,
          initialSnapshot: { messageId: newId(), baseDraftRevision: current.revision, baseSnapshotHash: "", snapshot: discloseSnapshot(binding.form, current.value) },
        };
        writeSessionValue(startStorageKey(currentSession.assistantSessionId), JSON.stringify(startAttempt.current));
      }
      const attempted = startAttempt.current;
      if (attempted.provider_id !== selection.providerId || attempted.model_id !== selection.modelId || attempted.expected_revision !== selection.revision || !equalJson(discloseSnapshot(binding.form, attempted.initialSnapshot.snapshot), attempted.initialSnapshot.snapshot)) {
        throw new Error("The prepared launch no longer matches this model or form. Choose Launch with current fields to authorize a new attempt.");
      }
      startInFlight.current = true;
      setBusy(true);
      setError(null);
      void (async () => {
        const request: AssistanceStartRequest = { ...attempted, initialSnapshot: { ...attempted.initialSnapshot, baseSnapshotHash: await snapshotHash(attempted.initialSnapshot.snapshot) } };
        const stillCurrent = () => canMutate() && expectedEpoch === authorityEpoch.current && !revocations.isEnded(currentSession.assistantSessionId);
        if (!stillCurrent()) return;
        startAttempt.current = request;
        writeSessionValue(startStorageKey(currentSession.assistantSessionId), JSON.stringify(request));
        await binding.flush();
        if (!canMutate() || expectedEpoch !== authorityEpoch.current || revocations.isEnded(currentSession.assistantSessionId)) throw new Error("Launch was cancelled because this draft or its model changed. Your form is preserved.");
        const result = normalizeAssistanceSession(await post("start", `assistance:${currentSession.assistantSessionId}`, `/api/assistance/sessions/${encodeURIComponent(currentSession.assistantSessionId)}/start`, request as unknown as Record<string, unknown>, stillCurrent));
        if (expectedEpoch !== authorityEpoch.current || !canMutate()) return;
        await acceptSession(result);
        if (result.phase === "active") {
          startAttempt.current = null;
          removeSessionValue(startStorageKey(currentSession.assistantSessionId));
        }
      })().catch((reason) => {
        if (!alive.current) return;
        setError(failure(reason));
        if (reason instanceof AssistanceRequestError && (reason.code === "execution_selection_changed" || reason.code === "assistance_control_changed")) executionProvider?.invalidate();
      }).finally(() => {
        startInFlight.current = false;
        if (alive.current) setBusy(false);
      });
    } catch (reason) { setError(failure(reason)); }
  };

  const endSession = (): void => {
    const current = sessionRef.current;
    if (!current) return;
    authorityEpoch.current += 1;
    revocations.revoke(binding.key, current.assistantSessionId);
    startAttempt.current = null;
    removeSessionValue(startStorageKey(current.assistantSessionId));
    const ended = { ...current, phase: "ended" as const, activeStartId: null };
    sessionRef.current = ended;
    setSession(ended);
  };

  const newSession = (): void => {
    const current = sessionRef.current;
    if (!current || !canMutate()) return;
    authorityEpoch.current += 1;
    revocations.revoke(binding.key, current.assistantSessionId);
    const history = [...historicalSessions.filter((item) => item.assistantSessionId !== current.assistantSessionId), current];
    setHistoricalSessions(history);
    writeSessionValue(historyKey, JSON.stringify(history));
    composerTransfer.current = readSessionValue(assistanceComposerKey(binding.key, current.assistantSessionId));
    removeSessionValue(storageKey);
    startAttempt.current = null;
    sessionRef.current = null;
    setSession(null);
    requestId.current = newId();
    void prepareSession();
  };

  const retain = (record: ReceiptRecord) => {
    recordsRef.current.set(record.patch.patchId, record);
    const next = [...recordsRef.current.values()];
    try {
      sessionStorage.setItem(receiptStorageKey(record.patch.assistantSessionId), JSON.stringify(next.filter((item) => item.patch.assistantSessionId === record.patch.assistantSessionId)));
      sessionStorage.setItem(journalKey, JSON.stringify(next));
    }
    catch { if (alive.current) setError("Local receipt recovery is unavailable after reload. Your form remains editable, and Undo remains available in this open panel."); }
    return next;
  };
  const publish = (record: ReceiptRecord) => {
    const next = retain({ ...record, stage: "committed" });
    setRecords(next);
    onRecords(binding.key, next);
  };
  const acknowledge = useCallback(async (receipt: DraftPatchReceipt, assistantSessionId?: string) => {
    const id = assistantSessionId ?? sessionRef.current?.assistantSessionId;
    if (!id) return;
    await post("acknowledge", `assistance:${id}`, `/api/assistance/${encodeURIComponent(id)}/receipts`, receipt as unknown as Record<string, unknown>);
  }, [post]);

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
    try { await acknowledge(receipt, patch.assistantSessionId); }
    catch (reason) { if (alive.current) setError(`Changes are visible in your form; their receipt will retry. ${failure(reason)}`); }
  };

  const applyPatch = async (unknownPatch: unknown, serverReceipt: DraftPatchReceipt | null, expectedEpoch: number) => {
    if (!session || !canMutate() || expectedEpoch !== authorityEpoch.current) return;
    const patch = await validatePatch(unknownPatch, { identity: binding.identity, assistantSessionId: session.assistantSessionId, conversationId: session.conversationId, form: binding.form });
    if (!canMutate() || expectedEpoch !== authorityEpoch.current) return;
    const local = recordsRef.current.get(patch.patchId);
    if (local) {
      if (serverReceipt && (serverReceipt.resultingRevision > local.receipt.resultingRevision || serverReceipt.status === "undone" || serverReceipt.status === "rejected")) {
        if (!equalJson(local.receipt, serverReceipt)) publish({ ...local, receipt: serverReceipt, changes: [] });
        return;
      }
      if (!serverReceipt || !equalJson(local.receipt, serverReceipt)) await acknowledge(local.receipt, patch.assistantSessionId);
      return;
    }
    if (serverReceipt) {
      // Receipted patches are never applied a second time after remount/reload.
      publish({ patch, receipt: serverReceipt, changes: [] });
      return;
    }
    await binding.flush();
    if (!canMutate() || expectedEpoch !== authorityEpoch.current) return;
    const current = binding.snapshot();
    // Old published evidence remains reviewable after Stop/switch/End, but a
    // fenced driver must not keep automatically editing the ordinary form.
    const form = canSend() ? binding.form : { ...binding.form, patchBehavior: "suggest" as const };
    const plan = planPatch(patch, form, current, binding.focused());
    await commitPlan(patch, plan);
  };

  const pollRef = useRef<() => Promise<void>>(async () => undefined);
  pollRef.current = async () => {
    const expected = sessionRef.current;
    const expectedEpoch = authorityEpoch.current;
    if (!expected || !canMutate() || polling.current || startInFlight.current) return;
    polling.current = true;
    try {
      const current = await client.session(expected.assistantSessionId);
      if (expectedEpoch !== authorityEpoch.current || sessionRef.current?.assistantSessionId !== expected.assistantSessionId) return;
      await acceptSession(current);
      const payload = await read(`/api/assistance/${encodeURIComponent(expected.assistantSessionId)}/patches`);
      if (expectedEpoch !== authorityEpoch.current || sessionRef.current?.assistantSessionId !== expected.assistantSessionId) return;
      const patches = isRecord(payload) && Array.isArray(payload.patches) ? payload.patches : [];
      for (const entry of patches) {
        if (!isRecord(entry) || expectedEpoch !== authorityEpoch.current) return;
        operationChain.current = operationChain.current.catch(() => undefined).then(() => applyPatch(entry.patch, entry.receipt as DraftPatchReceipt | null, expectedEpoch));
        await operationChain.current;
      }
    } catch (reason) { if (alive.current) setError(failure(reason)); }
    finally { polling.current = false; }
  };
  const poll = useCallback(() => { void pollRef.current(); }, []);
  const observeMessages = useCallback((messages: readonly ChatMessage[]) => {
    if (conversationId !== null) {
      setChatTranscript({ conversationId, messages });
    }
    poll();
  }, [conversationId, poll]);
  useEffect(() => {
    if (!session || !editable) return;
    poll();
    const timer = setInterval(poll, 3000);
    return () => clearInterval(timer);
  }, [editable, poll, sessionId]);

  const provider = useMemo(() => sessionId && conversationId ? new HttpChatConversationProvider({
    conversationId, fetchImpl: fetcher,
    basePath: `/api/assistance/${encodeURIComponent(sessionId)}/conversations`,
    authorizeSend: async (body) => {
      const epoch = authorityEpoch.current;
      if (!canSend()) throw new Error("Choose Launch before sending content to this assistant.");
      const authorized = await client.authorize("respond", `assistance:${sessionId}`, `/api/assistance/${encodeURIComponent(sessionId)}/conversations/${encodeURIComponent(conversationId)}/respond`, body);
      if (!canSend() || epoch !== authorityEpoch.current) throw new Error("This assistant was paused before the message could be sent.");
      return authorized;
    },
  }) : null, [binding, client, conversationId, fetcher, pauses, revocations, sessionId]);
  useEffect(() => {
    // A Start/Stop/switch can publish assistant context without a human send.
    // Refresh the canonical controller without replacing it or its composer.
    provider?.invalidate();
  }, [provider, session?.activeStartId, session?.phase]);
  useEffect(() => {
    if (!open) { startFocus.current = null; return; }
    if (busy || session?.phase !== "active") return;
    const trigger = startFocus.current;
    startFocus.current = null;
    if (trigger && (document.activeElement === trigger || (!trigger.isConnected && document.activeElement === document.body))) {
      // The assistance surface contains one canonical composer. A narrow DOM
      // ref hands off the disappearing Start action without custom chat input
      // plumbing, and never takes focus back from a human-edited form field.
      paneRef.current?.querySelector<HTMLTextAreaElement>("textarea:not(:disabled)")?.focus({ preventScroll: true });
    }
  }, [busy, open, session?.phase]);
  const composerStorageKey = sessionId ? assistanceComposerKey(binding.key, sessionId) : null;
  const retainedComposer = (): string => {
    try { return composerStorageKey ? sessionStorage.getItem(composerStorageKey) ?? "" : ""; } catch { return ""; }
  };

  const prepareSend = async (input: ChatSendInput): Promise<ChatSendInput> => {
    if (!session || !canSend() || !input.messageId) throw new Error("Assistance is paused. Your form is unchanged.");
    const epoch = authorityEpoch.current;
    let prepared = preparedRef.current.get(input.messageId);
    if (!prepared) {
      // Send authorizes the fields visible now, not edits made while their
      // persistence is pending. Freeze values/revision before the first await.
      const current = binding.snapshot();
      const snapshot = discloseSnapshot(binding.form, current.value);
      prepared = { messageId: input.messageId, baseDraftRevision: current.revision, baseSnapshotHash: "", snapshot };
      preparedRef.current.set(input.messageId, prepared);
    }
    // Retain the disclosure even when persistence or hashing fails. The
    // canonical composer's same-message retry must never recapture newer fields.
    await binding.flush();
    if (!canSend() || epoch !== authorityEpoch.current) throw new Error("This draft is no longer active.");
    if (!prepared.baseSnapshotHash) {
      const hash = await snapshotHash(prepared.snapshot);
      if (!canSend() || epoch !== authorityEpoch.current) throw new Error("This assistant is no longer active.");
      prepared = { ...prepared, baseSnapshotHash: hash };
      preparedRef.current.set(input.messageId, prepared);
    }
    if (!canSend() || epoch !== authorityEpoch.current) throw new Error("This assistant is no longer active.");
    await post("prepare", `assistance:${session.assistantSessionId}`, `/api/assistance/${encodeURIComponent(session.assistantSessionId)}/snapshots`, prepared as unknown as Record<string, unknown>, () => canSend() && epoch === authorityEpoch.current);
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
        await acknowledge(receipt, record.patch.assistantSessionId);
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
    <VisuallyHidden role="status" aria-live="polite">{records[records.length - 1]?.receipt.message}</VisuallyHidden>
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

  const terminal = session?.phase === "ended" || session?.phase === "expired" || session?.phase === "restart_required";
  const active = session?.phase === "active" && availability?.available === true && !stopPending;
  const selected = execution?.snapshot?.selection;
  const modelLabel = selected ? `${selected.providerLabel} · ${selected.modelLabel}` : "the selected model";
  const startDisabled = !editable || busy || stopPending || !Number.isSafeInteger(session?.controlRevision) || !availability?.available || execution?.status !== "ready" || execution.selecting || !execution.currentAvailable || execution.snapshot?.readOnly === true;
  const context = <HelpTarget content={{ summary: "AI help is bound to this form.", details: startAttempt.current ? "This launch keeps the fields and recent chat approved for its first attempt. In Jobs, it may also inspect the registered capability and workflow metadata already shown by the form. Later edits stay private; choose Launch with current fields to authorize them. Only you can submit the form." : "Launch shares its current allowlisted fields and bounded conversation history with the selected provider. In Jobs, it may also inspect the registered capability and workflow metadata already shown by the form. Later edits stay private until you send or launch again. Only you can submit the form." }} focusable placement="top start"><span className="wb-chat-composer__footer-accessory wb-assistance-context">About: {binding.form.title}</span></HelpTarget>;
  const launchRequired = !terminal && (!active || busy);
  const launchDisclosure = launchRequired && availability?.available ? <div className="wb-assistance-disclosure" role="group" aria-label="Launch disclosure">
    <HelpTarget content={{ summary: "Review what Launch shares.", details: availability.disclosure }} focusable placement="top start"><p>{startAttempt.current ? "Uses the context approved for this launch with " : "Launch shares this form's allowed context with "}<strong>{modelLabel}</strong>. Only you submit.</p></HelpTarget>
    {startAttempt.current && !busy && <HelpTarget content={{ summary: "Authorize the fields you see now.", details: "This creates a new launch attempt using the current fields, instead of retrying the previously authorized snapshot." }} reactAriaComposite><Button ref={freshStartButton} type="button" size="small" disabled={startDisabled} onClick={() => start(true)}>Launch with current fields</Button></HelpTarget>}
  </div> : undefined;
  const lifecycle = <div className="wb-assistance-lifecycle">
    {!editable && <InlineAlert tone="info" role="status">AI help is paused outside editable Operate mode. Your form is preserved.</InlineAlert>}
    {availability && !availability.available && <InlineAlert tone="info" role="status">{AVAILABILITY_LABELS[availability.code] ?? availability.message} <a href="/app/settings/system/dashboard-ai?setting=wb.dashboard.assistance">Dashboard AI settings</a></InlineAlert>}
    {error && <InlineAlert tone="danger" role="alert">{error}</InlineAlert>}
    {session?.agent?.error && session.agent.error !== error && <InlineAlert tone="danger" role="alert">{session.agent.error}</InlineAlert>}
    {terminal ? <div className="wb-assistance-start" role="status">
      <p>{session?.phase === "restart_required" ? "This conversation used the previous assistant. Its history and Undo remain available; a new AI help session needs a fresh disclosure and Launch." : "This AI help session has ended. Your form, conversation and Undo are preserved."}</p>
      <HelpTarget content={{ summary: "Prepare a new AI help conversation.", details: "The previous conversation stays in history. Review the selected model and disclosure, then choose Launch to share this form." }} reactAriaComposite><Button type="button" disabled={!editable || !availability?.available} onClick={newSession}>New AI help session</Button></HelpTarget>
    </div> : stopPending ? <p role="status">Stop has not been confirmed. Retry Stop before launching again.</p> : execution?.status === "ready" && !execution.currentAvailable ? <p role="status">Choose an available model to launch.</p> : session?.phase === "stopped" && !busy ? <span className="wb-assistance-state" role="status">Assistant stopped</span> : active && !busy ? <span className="wb-assistance-state" role="status">AI help active · Draft shaping only</span> : null}
    {session && session.phase !== "ended" && session.phase !== "expired" && <div className="wb-assistance-session-actions">
      {(active || busy || stopPending || startAttempt.current) && <HelpTarget content={{ summary: "Stop the assistant's current work.", details: "Stopping preserves the draft and conversation. A fresh explicit Launch is required before the assistant receives more content." }} reactAriaComposite><Button type="button" size="small" onClick={() => { void stop(); }}>{stopPending ? "Retry Stop" : "Stop assistant"}</Button></HelpTarget>}
      <details>
        <summary>Session actions</summary>
        <HelpTarget content={{ summary: "Permanently end this AI help session.", details: "Your form, conversation and conditional Undo stay here. This session cannot restart; a new session requires a new disclosure and Launch." }} reactAriaComposite><Button type="button" size="small" onClick={endSession}>End session and keep draft</Button></HelpTarget>
      </details>
    </div>}
  </div>;

  return <aside ref={paneRef} id={binding.paneId} className="wb-assistance-dock" aria-label="Draft assistance" hidden={!open} inert={!open ? true : undefined}>
    <header>
      <HelpTarget content={DRAFT_ASSISTANCE_HELP} focusable><h2 tabIndex={-1}>{binding.title()}</h2></HelpTarget>
      <div className="wb-assistance-dock__header-actions">
        {chatMessages.length > 0 && <ChatCopyAction messages={chatMessages} />}
        <HelpTarget content={{ summary: "Close the panel and keep your work.", details: "Closing this panel keeps your draft and conversation. Reopen AI help to continue." }} reactAriaComposite><IconButton label="Close assistance" title="" icon={<X weight="bold" />} variant="ghost" size="small" onClick={onClose} /></HelpTarget>
      </div>
    </header>
    <div className="wb-assistance-dock__body">
      {session && provider ? <ConversationChat
        provider={provider} conversationId={session.conversationId} title="Draft conversation"
        expectsInitialAssistantTurn={session.phase === "active" && session.activeStartId !== null && !stopPending}
        sendScopeKey={`${session.assistantSessionId}:${session.activeStartId ?? "unstarted"}:${session.controlRevision}:${session.execution?.selection.revision ?? "none"}:${session.phase}`}
        prepareSend={prepareSend} onMessagesChange={observeMessages} transcriptAppendix={appendix}
        composerDisabled={!editable || !active || busy} responsesDisabled={!editable || !active || busy}
        showStoppedNotice={false} showTranscriptCopyAction={false}
        execution={execution} executionDisabled={!editable || busy || terminal || !availability?.available}
        header={lifecycle} composerAccessory={launchDisclosure} composerFooterAccessory={context}
        composerPrimaryAction={launchRequired ? {
          label: startAttempt.current ? "Retry Launch" : "Launch", onAction: () => start(),
          disabled: startDisabled, pending: busy, pendingLabel: "Launching…", buttonRef: startButton,
          help: { summary: "Launch AI help for this form.", details: "Shares the disclosed fields and recent chat with the selected model. In Jobs, the assistant may also inspect the registered capability and workflow metadata already shown in the form. It can suggest field edits, but only you can submit. Launch does not send any unsent message." },
        } : undefined}
        noMessagesLabel="Your conversation will appear here."
        composerPlaceholder={active ? "Ask about this form…" : "Launch to chat about this form…"}
        readOnlyReason="This AI help conversation is read-only. Your form remains editable."
        initialValue={retainedComposer()} onDraftChange={(value) => { if (composerStorageKey) writeSessionValue(composerStorageKey, value); }}
      /> : <ChatPanelState
        label="Draft conversation" kind={preparing ? "loading" : error ? "error" : "empty"}
        title={preparing ? "Preparing AI help…" : error ? "AI help could not load" : availability ? AVAILABILITY_LABELS[availability.code] : "AI help is unavailable"}
        detail={preparing ? undefined : error ?? <>{availability?.message} {!availability?.available && <a href="/app/settings/system/dashboard-ai?setting=wb.dashboard.assistance">Set up form assistance</a>}</>}
        action={preparing ? undefined : { label: "Retry availability", onAction: () => { void load(); } }}
      />}
    </div>
    {historicalSessions.length > 0 && <details className="wb-assistance-history" open={historyOpen} onToggle={(event) => setHistoryOpen(event.currentTarget.open)}>
      <summary>Previous AI help conversations</summary>
      {historyOpen && historicalSessions.map((previous) => <HistoricalConversation key={previous.assistantSessionId} session={previous} client={client} />)}
    </details>}
  </aside>;
}

/** Explicit historical inspection still uses the canonical, read-only Chat. */
function HistoricalConversation({ session, client }: { readonly session: AssistanceSession; readonly client: AssistanceClient }) {
  const provider = useMemo<ChatConversationProvider>(() => {
    const transport = new HttpChatConversationProvider({
      conversationId: session.conversationId, fetchImpl: client.fetcher,
      basePath: `/api/assistance/${encodeURIComponent(session.assistantSessionId)}/conversations`,
    });
    return {
      loadConversation: async (id) => ({ ...await transport.loadConversation(id), status: "closed" }),
      sendMessage: async () => { throw new Error("Historical assistance is read-only."); },
      subscribe: (id, invalidate) => transport.subscribe(id, invalidate),
    };
  }, [client, session.assistantSessionId, session.conversationId]);
  return <div className="wb-assistance-history__conversation"><ConversationChat provider={provider} conversationId={session.conversationId} title="Previous AI help conversation" readOnlyReason="This history is preserved. Launch a new session to continue with the form." /></div>;
}
