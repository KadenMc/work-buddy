import { Plus, X } from "@phosphor-icons/react";
import { useEffect, useId, useRef, useState } from "react";
import { Dialog, DialogTrigger, Heading, Modal, ModalOverlay, Popover } from "react-aria-components";
import type { IntentResult } from "../../../dashboard/contributions/contracts";
import { InlineAlert } from "../../../ui";
import { createCorrelationId } from "../../../widget-library/shared";
import { TaskButton as Button } from "./TaskHelp";
import "./TaskNamespacePills.css";

export interface TaskNamespacePillsProps {
  readonly taskId: string;
  readonly namespaces: readonly string[];
  readonly options: readonly { readonly value: string; readonly label: string }[];
  readonly readOnly?: boolean;
  /** Pause new assignments while retaining an already reviewed retry. */
  readonly disabled?: boolean;
  readonly compact?: boolean;
  onChange(nextNamespaces: readonly string[], mutationId?: string): Promise<IntentResult>;
}

interface NamespaceChange {
  readonly taskId: string;
  readonly name: string;
  readonly next: readonly string[];
  readonly mutationId: string;
  readonly commit: TaskNamespacePillsProps["onChange"];
  readonly sourceKey: string;
  readonly skipConfirmations?: boolean;
}
const displayNamespace = (path: string) => path.endsWith("/") ? `${path} (empty segment)` : path;
const cooldownDuration = 10 * 60 * 1000;
const cooldownEvent = "wb.tasks.namespace-confirmations-changed";
const cooldownFallback = new Map<string, number>();
const cooldownKey = (taskId: string) => `wb.tasks.namespace-confirmations:${encodeURIComponent(taskId)}`;
const readCooldown = (taskId: string): number => {
  let expires = cooldownFallback.get(taskId) ?? 0;
  if (!cooldownFallback.has(taskId)) {
    try { expires = Number(sessionStorage.getItem(cooldownKey(taskId)) ?? 0); } catch { /* This session can still use the in-memory preference. */ }
  }
  return Number.isFinite(expires) && expires > Date.now() && expires <= Date.now() + cooldownDuration ? expires : 0;
};
const writeCooldown = (taskId: string, expires: number) => {
  try { sessionStorage.setItem(cooldownKey(taskId), String(expires)); cooldownFallback.delete(taskId); }
  catch { cooldownFallback.set(taskId, expires); }
  window.dispatchEvent(new Event(cooldownEvent));
};
function useRemovalCooldown(taskId: string) {
  const [preference, setPreference] = useState(() => ({ taskId, expires: readCooldown(taskId) }));
  const expires = preference.taskId === taskId ? preference.expires : readCooldown(taskId);
  useEffect(() => {
    const refresh = () => setPreference({ taskId, expires: readCooldown(taskId) });
    refresh();
    window.addEventListener(cooldownEvent, refresh);
    window.addEventListener("storage", refresh);
    return () => { window.removeEventListener(cooldownEvent, refresh); window.removeEventListener("storage", refresh); };
  }, [taskId]);
  useEffect(() => {
    if (!expires) return;
    const timer = setTimeout(() => setPreference({ taskId, expires: readCooldown(taskId) }), Math.max(0, expires - Date.now()));
    return () => clearTimeout(timer);
  }, [taskId, expires]);
  return expires > Date.now();
}

/** A manual assignment change always targets one task and retains its reviewed callback. */
export function TaskNamespacePills(props: TaskNamespacePillsProps) {
  return <TaskNamespacePillsEditor key={props.taskId} {...props} />;
}

function TaskNamespacePillsEditor({ taskId, namespaces, options, readOnly = false, disabled = false, compact = false, onChange }: TaskNamespacePillsProps) {
  const confirmationsSkipped = useRemovalCooldown(taskId);
  const [optimistic, setOptimistic] = useState<readonly string[] | null>(null);
  const current = optimistic ?? namespaces;
  const namespaceKey = JSON.stringify(namespaces);
  const latestNamespacesKey = useRef(namespaceKey);
  latestNamespacesKey.current = namespaceKey;
  useEffect(() => setOptimistic(null), [namespaceKey]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [removal, setRemoval] = useState<NamespaceChange | null>(null);
  const [removalVisible, setRemovalVisible] = useState(false);
  const [skipChoice, setSkipChoice] = useState(false);
  const [addition, setAddition] = useState<NamespaceChange | null>(null);
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const addRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if ((removal || addition) && !busy && (conflict || readOnly)) cancelRef.current?.focus();
  }, [removal, addition, busy, conflict, readOnly]);
  const titleId = useId(); const descriptionId = useId();
  const uniqueOptions = [...new Map(options.filter((option) => option.value && !option.value.startsWith("__")).map((option) => [option.value, option])).values()];
  const needle = search.trim().toLowerCase();
  const existing = uniqueOptions.filter((option) => !current.includes(option.value) && `${option.value} ${option.label}`.toLowerCase().includes(needle));
  const candidate = search.trim().replace(/^#+/, "").toLowerCase();
  const validCandidate = /^[a-z0-9][a-z0-9_/-]*$/.test(candidate) && candidate.split("/").every(Boolean);
  const knownCandidate = uniqueOptions.some((option) => option.value === candidate) || current.includes(candidate);
  const freeze = (name: string, next: readonly string[]): NamespaceChange => ({ taskId, name, next: [...next], mutationId: createCorrelationId("task-namespace"), commit: onChange, sourceKey: namespaceKey });
  const clearNotice = () => { setError(null); setConflict(false); };
  const changePicker = (open: boolean) => {
    if (pending.current) return;
    setPickerOpen(open);
    if (!open) { setAddition(null); setSearch(""); clearNotice(); }
  };
  const apply = async (change: NamespaceChange, kind: "add" | "remove") => {
    if (pending.current || readOnly || conflict) return;
    pending.current = true; setBusy(true); setError(null);
    try {
      const result = await change.commit(change.next, change.mutationId);
      if (result.status === "accepted") {
        if (kind === "remove" && change.skipConfirmations) writeCooldown(change.taskId, Date.now() + cooldownDuration);
        // A replayed receipt must not hide newer assignments received while
        // this request was in flight. Prefer that authoritative snapshot.
        setOptimistic(latestNamespacesKey.current === change.sourceKey || latestNamespacesKey.current === JSON.stringify(change.next) ? change.next : null);
        setPickerOpen(false); setRemoval(null); setAddition(null); setSearch("");
        if (kind === "remove") requestAnimationFrame(() => addRef.current?.focus());
      } else {
        setConflict(result.status === "conflict");
        setError(result.message ?? "The namespace change could not be confirmed.");
        if (kind === "remove") setRemovalVisible(true);
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The namespace change could not be confirmed.");
      if (kind === "remove") setRemovalVisible(true);
    } finally { pending.current = false; setBusy(false); }
  };
  const add = (name: string) => {
    if (pending.current || readOnly || disabled || addition || current.includes(name)) return;
    clearNotice();
    const change = freeze(name, [...current, name]);
    setAddition(change); void apply(change, "add");
  };
  const remove = (name: string) => {
    if (pending.current || readOnly || disabled) return;
    clearNotice(); setSkipChoice(false);
    const change = freeze(name, current.filter((path) => path !== name));
    const skip = readCooldown(taskId) > Date.now();
    const reviewed = skip ? { ...change, skipConfirmations: false } : change;
    setRemoval(reviewed); setRemovalVisible(!skip);
    if (skip) void apply(reviewed, "remove");
  };
  const confirmRemoval = () => {
    if (!removal) return;
    const reviewed = removal.skipConfirmations === undefined ? { ...removal, skipConfirmations: skipChoice } : removal;
    setRemoval(reviewed); void apply(reviewed, "remove");
  };
  const notice = error ? <InlineAlert tone={conflict ? "warning" : "danger"}>{error} {conflict ? "Cancel and review the latest namespaces before trying again." : "Retrying uses the same reviewed change."}</InlineAlert> : null;
  return <div className={`wb-task-namespace-pills${compact ? " wb-task-namespace-pills--compact" : ""}`} role="group" aria-label="Task namespaces">
    <DialogTrigger isOpen={pickerOpen} onOpenChange={changePicker}>
      <Button ref={addRef} className="wb-task-namespace-pills__add" size="small" variant="ghost" disabled={readOnly || disabled || busy} aria-label="Add namespace to this task" help={{ summary: "Choose a namespace to add to this task.", details: "Search existing namespaces or explicitly create a new path. Selecting an existing namespace adds its assignment to this task only; project links and other tasks stay unchanged." }}><Plus aria-hidden="true" /> Add</Button>
      <Popover className="wb-popover wb-task-namespace-pills__popover" placement="bottom start" containerPadding={12} offset={5} shouldFlip shouldCloseOnInteractOutside={() => !pending.current} isKeyboardDismissDisabled={busy}>
        <Dialog className="wb-task-namespace-pills__picker" aria-label="Add namespace">
          <Heading slot="title">Add a namespace</Heading>
          <label className="wb-task-field"><span>Find or create namespace</span><input autoFocus type="search" value={search} disabled={busy || addition !== null} onChange={(event) => setSearch(event.target.value)} placeholder="Search existing paths…" /></label>
          {notice}
          {addition ? <><p>{busy ? "Adding" : "Reviewed addition"}: <strong>{displayNamespace(addition.name)}</strong></p>{error && !conflict ? <Button disabled={busy || readOnly} onClick={() => void apply(addition, "add")}>Retry adding namespace</Button> : null}</> : <>
            <ul className="wb-task-namespace-pills__options" aria-label="Existing namespaces">{existing.map((option) => <li key={option.value}><Button size="small" variant="ghost" disabled={readOnly || disabled} aria-label={`Add ${option.value} to this task`} onClick={() => add(option.value)}>{displayNamespace(option.value)}</Button></li>)}</ul>
            {existing.length === 0 ? <p className="wb-task-muted">No unassigned namespaces match.</p> : null}
            {candidate && !knownCandidate ? <div className="wb-task-namespace-pills__create"><p>Create <strong>{candidate}</strong> and assign it to this task.</p>{!validCandidate ? <p className="wb-task-muted">Use letters, numbers, hyphens or underscores, with a name on each side of every slash.</p> : null}<Button disabled={readOnly || disabled || !validCandidate} onClick={() => add(candidate)}>Create and add namespace</Button></div> : null}
            {current.includes(candidate) ? <p className="wb-task-muted">This namespace is already assigned.</p> : null}
          </>}
          <Button ref={cancelRef} disabled={busy} onClick={() => changePicker(false)}>Cancel</Button>
        </Dialog>
      </Popover>
    </DialogTrigger>
    {confirmationsSkipped ? <Button className="wb-task-namespace-pills__preference" size="small" variant="ghost" disabled={busy} aria-label="Resume namespace removal confirmations for this task" help={{ summary: "Resume namespace removal confirmations for this task.", details: "Namespace removals currently save immediately for this task in this browser session. This temporary preference expires ten minutes after the confirmed opt-in. Completion still always asks for confirmation." }} onClick={() => writeCooldown(taskId, 0)}>Confirmations off</Button> : null}
    {busy && removal && !removalVisible ? <span role="status" className="wb-task-namespace-pills__empty">Removing {displayNamespace(removal.name)}…</span> : null}
    {current.map((name) => <span className="wb-task-namespace-pills__pill" key={name}>
      <span title={name}>{displayNamespace(name)}</span>
      <Button size="small" variant="ghost" disabled={readOnly || disabled || busy} aria-label={`Remove ${name} from this task`} help={confirmationsSkipped ? { summary: `Remove ${name} from this task immediately.`, details: "You temporarily skipped namespace removal confirmations for this task. This changes only its assignment; you can add it back with Add. Choose Confirmations off to resume confirmations." } : { summary: `Review removing ${name} from this task.`, details: "The confirmation affects only this task’s assignment. It does not delete the namespace or change other tasks. Cancel keeps the assignment." }} onClick={() => remove(name)}><X aria-hidden="true" /></Button>
    </span>)}
    {current.length === 0 ? <span className="wb-task-namespace-pills__empty">No namespaces</span> : null}
    {removal && removalVisible ? <ModalOverlay isOpen isDismissable={!busy} isKeyboardDismissDisabled={busy} onOpenChange={(open) => { if (!open && !pending.current) { setRemoval(null); clearNotice(); } }} className="wb-task-confirm-overlay">
      <Modal className="wb-task-confirm-modal"><Dialog className="wb-task-confirm-dialog" role="alertdialog" aria-labelledby={titleId} aria-describedby={descriptionId} aria-busy={busy || undefined}>
        <Heading id={titleId} slot="title">Remove namespace from this task?</Heading>
        <p className="wb-task-confirm-name">{displayNamespace(removal.name)}</p>
        <p id={descriptionId}>This removes only this task’s assignment. The namespace and other tasks remain unchanged. You can add it back with Add.</p>
        {removal.skipConfirmations === undefined || removal.skipConfirmations ? <label className="wb-task-namespace-pills__confirmation-choice"><input type="checkbox" checked={skipChoice} disabled={busy || removal.skipConfirmations !== undefined} onChange={(event) => setSkipChoice(event.target.checked)} /><span>Skip namespace removal confirmations for this task for 10 minutes</span></label> : null}
        {notice}
        <div className="wb-task-actions"><Button ref={cancelRef} autoFocus disabled={busy} onClick={() => { setRemoval(null); clearNotice(); }}>Cancel</Button><Button variant="primary" disabled={readOnly || busy || conflict} onClick={confirmRemoval}>{busy ? "Removing…" : error ? "Retry removal" : "Remove namespace"}</Button></div>
      </Dialog></Modal>
    </ModalOverlay> : null}
  </div>;
}
