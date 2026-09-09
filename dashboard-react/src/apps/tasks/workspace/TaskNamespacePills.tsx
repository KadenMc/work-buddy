import { Plus, X } from "@phosphor-icons/react";
import { useEffect, useId, useRef, useState } from "react";
import { Dialog, DialogTrigger, Heading, Modal, ModalOverlay, Popover } from "react-aria-components";
import type { IntentResult } from "../../../dashboard/contributions/contracts";
import { InlineAlert } from "../../../ui";
import { createCorrelationId } from "../../../widget-library/shared";
import { TaskButton as Button } from "./TaskHelp";
import "./TaskNamespacePills.css";

export interface TaskNamespacePillsProps {
  readonly namespaces: readonly string[];
  readonly options: readonly { readonly value: string; readonly label: string }[];
  readonly readOnly?: boolean;
  /** Pause new assignments while retaining an already reviewed retry. */
  readonly disabled?: boolean;
  readonly compact?: boolean;
  onChange(nextNamespaces: readonly string[], mutationId?: string): Promise<IntentResult>;
}

interface NamespaceChange {
  readonly name: string;
  readonly next: readonly string[];
  readonly mutationId: string;
  readonly commit: TaskNamespacePillsProps["onChange"];
  readonly sourceKey: string;
}
const displayNamespace = (path: string) => path.endsWith("/") ? `${path} (empty segment)` : path;

/** A manual assignment change always targets one task and retains its reviewed callback. */
export function TaskNamespacePills({ namespaces, options, readOnly = false, disabled = false, compact = false, onChange }: TaskNamespacePillsProps) {
  const [optimistic, setOptimistic] = useState<readonly string[] | null>(null);
  const current = optimistic ?? namespaces;
  const namespaceKey = JSON.stringify(namespaces);
  const latestNamespacesKey = useRef(namespaceKey);
  latestNamespacesKey.current = namespaceKey;
  useEffect(() => setOptimistic(null), [namespaceKey]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [removal, setRemoval] = useState<NamespaceChange | null>(null);
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
  const freeze = (name: string, next: readonly string[]): NamespaceChange => ({ name, next: [...next], mutationId: createCorrelationId("task-namespace"), commit: onChange, sourceKey: namespaceKey });
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
        // A replayed receipt must not hide newer assignments received while
        // this request was in flight. Prefer that authoritative snapshot.
        setOptimistic(latestNamespacesKey.current === change.sourceKey || latestNamespacesKey.current === JSON.stringify(change.next) ? change.next : null);
        setPickerOpen(false); setRemoval(null); setAddition(null); setSearch("");
        if (kind === "remove") requestAnimationFrame(() => addRef.current?.focus());
      } else {
        setConflict(result.status === "conflict");
        setError(result.message ?? "The namespace change could not be confirmed.");
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The namespace change could not be confirmed.");
    } finally { pending.current = false; setBusy(false); }
  };
  const add = (name: string) => {
    if (pending.current || readOnly || disabled || addition || current.includes(name)) return;
    clearNotice();
    const change = freeze(name, [...current, name]);
    setAddition(change); void apply(change, "add");
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
    {current.map((name) => <span className="wb-task-namespace-pills__pill" key={name}>
      <span title={name}>{displayNamespace(name)}</span>
      <Button size="small" variant="ghost" disabled={readOnly || disabled || busy} aria-label={`Remove ${name} from this task`} help={{ summary: `Review removing ${name} from this task.`, details: "The confirmation affects only this task’s assignment. It does not delete the namespace or change other tasks. Cancel keeps the assignment." }} onClick={() => { clearNotice(); setRemoval(freeze(name, current.filter((path) => path !== name))); }}><X aria-hidden="true" /></Button>
    </span>)}
    {current.length === 0 ? <span className="wb-task-namespace-pills__empty">No namespaces</span> : null}
    {removal ? <ModalOverlay isOpen isDismissable={!busy} isKeyboardDismissDisabled={busy} onOpenChange={(open) => { if (!open && !pending.current) { setRemoval(null); clearNotice(); } }} className="wb-task-confirm-overlay">
      <Modal className="wb-task-confirm-modal"><Dialog className="wb-task-confirm-dialog" role="alertdialog" aria-labelledby={titleId} aria-describedby={descriptionId} aria-busy={busy || undefined}>
        <Heading id={titleId} slot="title">Remove namespace from this task?</Heading>
        <p className="wb-task-confirm-name">{displayNamespace(removal.name)}</p>
        <p id={descriptionId}>This removes only this task’s assignment. The namespace and other tasks remain unchanged. You can add it back with Add.</p>
        {notice}
        <div className="wb-task-actions"><Button ref={cancelRef} autoFocus disabled={busy} onClick={() => { setRemoval(null); clearNotice(); }}>Cancel</Button><Button variant="primary" disabled={readOnly || busy || conflict} onClick={() => void apply(removal, "remove")}>{busy ? "Removing…" : error ? "Retry removal" : "Remove namespace"}</Button></div>
      </Dialog></Modal>
    </ModalOverlay> : null}
  </div>;
}
