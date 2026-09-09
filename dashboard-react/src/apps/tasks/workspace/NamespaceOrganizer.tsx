import { useEffect, useRef, useState } from "react";
import { InlineAlert } from "../../../ui";
import { TaskButton as Button, TaskHelp, TASK_HELP } from "./TaskHelp";
import type { IntentResult, JsonValue } from "../../../dashboard/contributions/contracts";
import { createCorrelationId } from "../../../widget-library/shared";
import { TASK_INTENTS, type TaskNamespaceNode } from "../contracts";
import { MultiSelect, toggleValue } from "./TaskFilters";

interface Operation { operation_id: string; action: string; label: string; created_at: string; undone_at: string | null; can_undo: boolean }
interface Preview {
  request: Record<string, JsonValue>; fingerprint: string; collection_revision: number; action: string;
  mapping: { from: string; to: string | null; task_count: number; collision: boolean }[];
  task_count: number; assignment_count: number; status_counts: Record<string, number>;
  unnamespaced_count: number; collisions: { from: string; to: string }[]; can_apply: boolean; issues: string[];
  assignments_added?: number; assignments_removed?: number; duplicate_assignments_removed?: number;
  tasks: { task_id: string; title: string; revision: number; before: string[]; after: string[] }[];
  tasks_offset: number; tasks_limit: number; tasks_has_more: boolean; scope: string;
}
type Action = "rename" | "move" | "merge" | "promote" | "remove" | "assign";
const labels: Record<Action, string> = { rename: "Rename", move: "Move", merge: "Merge", promote: "Move children up one level", remove: "Remove assignments", assign: "Change namespaces" };
const asRecord = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const compareNamespacePaths = (left: TaskNamespaceNode, right: TaskNamespaceNode) => {
  const a = left.path.split("/"); const b = right.path.split("/");
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    const comparison = a[index]!.localeCompare(b[index]!);
    if (comparison !== 0) return comparison;
  }
  return a.length - b.length;
};

export function NamespaceOrganizer({ send, readOnly, selectedTaskIds: initialSelectedTaskIds, onClose, onApplied }: {
  send(type: string, payload: JsonValue, mutation?: boolean, reuseId?: string): Promise<IntentResult>;
  readonly readOnly: boolean; readonly selectedTaskIds?: readonly string[]; onClose(): void; onApplied(): void;
}) {
  const selectedTaskIds = useRef(initialSelectedTaskIds).current;
  const [nodes, setNodes] = useState<readonly TaskNamespaceNode[]>([]);
  const [operations, setOperations] = useState<readonly Operation[]>([]);
  const [search, setSearch] = useState("");
  const [sources, setSources] = useState<readonly string[]>([]);
  const [action, setAction] = useState<Action | null>(selectedTaskIds ? "assign" : null);
  const [name, setName] = useState("");
  const [destination, setDestination] = useState("");
  const [includeDescendants, setIncludeDescendants] = useState(true);
  const [parentAssignment, setParentAssignment] = useState<"" | "keep" | "move">("");
  const [parentDestination, setParentDestination] = useState("");
  const [assignmentMode, setAssignmentMode] = useState("add");
  const [assignments, setAssignments] = useState<readonly string[]>([]);
  const [newNamespace, setNewNamespace] = useState("");
  const [mergeCollisions, setMergeCollisions] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success" | "warning"; text: string } | null>(null);
  const [applied, setApplied] = useState<Operation | null>(null);
  const [inspecting, setInspecting] = useState(false);
  const pendingApply = useRef<{ id: string; payload: JsonValue } | null>(null);
  const pendingUndo = useRef<{ id: string; operationId: string } | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const sendRef = useRef(send); sendRef.current = send;
  const inventory = async () => {
    const result = await sendRef.current(TASK_INTENTS.namespaceLoad, {});
    if (result.status !== "accepted") throw new Error(result.message ?? "Namespaces could not load.");
    const data = asRecord(result.value);
    setNodes(Array.isArray(data.namespaces) ? data.namespaces as TaskNamespaceNode[] : []);
    setOperations(Array.isArray(data.operations) ? data.operations as Operation[] : []);
  };
  useEffect(() => { setBusy(true); void inventory().catch((error: unknown) => setMessage({ tone: "danger", text: error instanceof Error ? error.message : "Namespaces could not load." })).finally(() => setBusy(false)); headingRef.current?.focus(); }, []);
  const request = (): Record<string, JsonValue> => ({
    action: action ?? "remove", sources: [...sources], include_descendants: includeDescendants,
    merge_collisions: mergeCollisions,
    ...(action === "rename" ? { name } : {}),
    ...(action === "move" || action === "merge" ? { destination } : {}),
    ...(action === "promote" ? { ...(parentAssignment ? { parent_assignment: parentAssignment } : {}), ...(parentAssignment === "move" ? { parent_destination: parentDestination } : {}) } : {}),
    ...(action === "assign" ? { task_ids: [...(selectedTaskIds ?? [])], assignment_mode: assignmentMode, namespaces: [...assignments] } : {}),
  });
  const getPreview = async (change?: Record<string, JsonValue>, offset = 0) => {
    setBusy(true); setMessage(null);
    try {
      const result = await sendRef.current(TASK_INTENTS.namespacePreview, { ...(change ?? request()), tasks_offset: offset, tasks_limit: 25 });
      if (result.status !== "accepted") throw new Error(result.message ?? "The change could not be previewed.");
      const candidate = asRecord(result.value).preview as Preview | undefined;
      if (!candidate?.fingerprint || !Array.isArray(candidate.mapping) || !Array.isArray(candidate.tasks)) throw new Error("The namespace preview is incomplete.");
      setPreview(candidate); setReviewing(true); pendingApply.current = null;
      requestAnimationFrame(() => headingRef.current?.focus());
    } catch (error) { setMessage({ tone: "danger", text: error instanceof Error ? error.message : "The change could not be previewed." }); }
    finally { setBusy(false); }
  };
  const apply = async () => {
    if (!preview?.can_apply || busy || readOnly) return;
    setBusy(true); setMessage(null);
    if (!pendingApply.current) pendingApply.current = { id: createCorrelationId("namespace-apply"), payload: { request: preview.request, expected_fingerprint: preview.fingerprint } };
    try {
      const result = await sendRef.current(TASK_INTENTS.namespaceApply, pendingApply.current.payload, true, pendingApply.current.id);
      if (result.status !== "accepted") { if (result.status === "conflict") { pendingApply.current = null; setPreview({ ...preview, can_apply: false }); } throw new Error(result.message ?? "The change could not be confirmed. Retry to check the same operation."); }
      const operation = asRecord(result.value).operation as Operation;
      setApplied(operation); setReviewing(false); setPreview(null); pendingApply.current = null;
      if (!selectedTaskIds) {
        setSources(sources.map((source) => preview.mapping.find((row) => row.from === source)?.to ?? source).filter((path): path is string => Boolean(path)));
        setAction(null);
      }
      setMessage({ tone: "success", text: `${operation.label}. Project links and task identities were preserved.` });
      onApplied(); await inventory();
    } catch (error) { setMessage({ tone: "danger", text: error instanceof Error ? error.message : "The change could not be confirmed. Retry the same operation." }); }
    finally { setBusy(false); }
  };
  const undo = async (operation: Operation) => {
    setBusy(true); setMessage(null);
    if (pendingUndo.current?.operationId !== operation.operation_id) pendingUndo.current = { operationId: operation.operation_id, id: createCorrelationId("namespace-undo") };
    try {
      const result = await sendRef.current(TASK_INTENTS.namespaceUndo, { operation_id: operation.operation_id }, true, pendingUndo.current.id);
      if (result.status !== "accepted") throw new Error(result.message ?? "Undo could not be confirmed.");
      setMessage({ tone: "success", text: "Namespace change undone." }); pendingUndo.current = null; setApplied(null); await inventory();
    } catch (error) { setMessage({ tone: "danger", text: error instanceof Error ? error.message : "Undo could not be confirmed." }); }
    finally { setBusy(false); }
  };
  const chooseAction = (next: Action) => { setAction(next); setName(next === "rename" ? sources[0]?.split("/").slice(-1)[0] ?? "" : ""); setPreview(null); setReviewing(false); setMergeCollisions(false); setMessage(null); setApplied(null); };
  const destinationOptions = nodes.filter((node) => !sources.some((source) => node.path === source || node.path.startsWith(`${source}/`)));
  const sourceDirect = nodes.filter((node) => sources.includes(node.path)).reduce((count, node) => count + node.direct_count, 0);

  return <section className="wb-task-organizer" aria-label={selectedTaskIds ? "Change selected tasks’ namespaces" : "Namespace organizer"}>
    <div className="wb-task-section-heading"><div><p className="wb-task-muted">Task organization</p><h2 ref={headingRef} tabIndex={-1}>{reviewing ? `Review ${action ? labels[action].toLowerCase() : "change"}` : selectedTaskIds ? "Change namespaces" : "Manage namespaces"}</h2></div><Button size="small" disabled={busy} onClick={onClose}>Back to tasks</Button></div>
    <p>{selectedTaskIds ? `${selectedTaskIds.length} selected tasks. Add, remove, or replace their namespace assignments.` : "Organize namespaces across all tasks, including Completed, Archived, and Trash. Select a namespace to choose an action."} Project links remain independent.</p>
    {message ? <InlineAlert tone={message.tone}>{message.text}</InlineAlert> : null}
    {applied?.can_undo ? <Button help={TASK_HELP.undoNamespaces} disabled={readOnly || busy} onClick={() => void undo(applied)}>Undo {applied.label.toLowerCase()}</Button> : null}
    {reviewing && preview ? <div className="wb-task-namespace-review">
      <div className="wb-task-preview-stats"><strong>{preview.task_count} tasks</strong><span>{preview.assignment_count} assignments</span><span>{preview.unnamespaced_count} tasks left without a namespace</span></div>
      <p>{preview.assignments_added ?? 0} assignments added · {preview.assignments_removed ?? 0} removed · {preview.duplicate_assignments_removed ?? 0} duplicate assignments combined</p>
      <p>Scope: {preview.scope === "selected_tasks" ? "selected tasks" : "all lifecycle records"}. {Object.entries(preview.status_counts).map(([status, count]) => `${count} ${status}`).join(" · ")}</p>
      {preview.issues.length ? <InlineAlert tone="warning"><ul>{preview.issues.map((issue) => <li key={issue}>{issue}</li>)}</ul></InlineAlert> : null}
      <div className="wb-task-table-scroll"><table><caption>Namespace changes</caption><thead><tr><th>Before</th><th>After</th><th>Direct tasks</th></tr></thead><tbody>{preview.mapping.map((row) => <tr key={`${row.from}:${row.to}`}><td>{row.from || "No namespace"}</td><td>{row.to ?? "Assignment removed"}{row.collision ? <strong> · existing destination</strong> : null}</td><td>{row.task_count}</td></tr>)}</tbody></table></div>
      {preview.collisions.length > 0 && (action === "rename" || action === "move" || action === "promote") ? <label className="wb-task-checkbox"><input type="checkbox" checked={mergeCollisions} disabled={busy} onChange={(event) => { setMergeCollisions(event.target.checked); void getPreview({ ...preview.request, merge_collisions: event.target.checked }); }} /><span>Merge assignments into the existing destinations shown above. Duplicate assignments become one; tasks remain separate.</span></label> : null}
      <Button size="small" onClick={() => setInspecting(!inspecting)}>{inspecting ? "Hide affected tasks" : "Inspect affected tasks"}</Button>
      {inspecting ? <><ul className="wb-task-affected-list">{preview.tasks.map((task) => <li key={task.task_id}><strong>{task.title}</strong><span>{task.before.join(", ") || "No namespace"} → {task.after.join(", ") || "No namespace"}</span></li>)}</ul><div className="wb-task-pagination"><Button size="small" disabled={busy || preview.tasks_offset === 0} onClick={() => void getPreview(preview.request, Math.max(0, preview.tasks_offset - preview.tasks_limit))}>Previous tasks</Button><span>{preview.tasks_offset + (preview.tasks.length ? 1 : 0)}–{preview.tasks_offset + preview.tasks.length} of {preview.task_count}</span><Button size="small" disabled={busy || !preview.tasks_has_more} onClick={() => void getPreview(preview.request, preview.tasks_offset + preview.tasks_limit)}>More affected tasks</Button></div></> : null}
      <div className="wb-task-actions"><Button disabled={busy} onClick={() => { setReviewing(false); }}>Back to edit</Button><Button variant="ghost" disabled={busy} onClick={() => { setReviewing(false); setPreview(null); pendingApply.current = null; if (!selectedTaskIds) setAction(null); }}>Cancel change</Button><Button disabled={busy} onClick={() => void getPreview()}>Refresh preview</Button><Button help={TASK_HELP.apply} variant="primary" disabled={readOnly || busy || !preview.can_apply} onClick={() => void apply()}>{busy ? "Working…" : pendingApply.current ? "Retry same change" : `${action ? labels[action] : "Apply change"} · ${preview.task_count} tasks`}</Button></div>
    </div> : <>
      {!selectedTaskIds && !action ? <div className="wb-task-namespace-inventory">
        <div className="wb-task-actions" aria-label="Namespace actions">{(["rename", "move", "merge", "promote", "remove"] as const).map((next) => <Button key={next} size="small" disabled={busy || sources.length === 0 || ((next === "rename" || next === "promote") && sources.length !== 1)} onClick={() => chooseAction(next)}>{labels[next]}</Button>)}</div>
        {sources.length ? <p>{sources.length} selected · {sources.join(", ")}</p> : null}
        <label className="wb-task-field"><span>Find namespaces</span><input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search all namespaces…" /></label>
        <div className="wb-task-inventory-list" role="group" aria-label="All namespaces">{[...nodes].sort(compareNamespacePaths).filter((node) => sources.includes(node.path) || node.path.toLowerCase().includes(search.toLowerCase())).map((node) => <label className="wb-task-checkbox" key={node.path} style={{ paddingInlineStart: `${Math.min(node.path.split("/").length - 1, 5) * 16}px` }}><TaskHelp content={TASK_HELP.namespaceSelection}><input type="checkbox" aria-label={node.path} checked={sources.includes(node.path)} disabled={busy} onChange={() => { setSources(toggleValue(sources, node.path)); setAction(null); }} /></TaskHelp><span>{node.path}<small>{node.direct_count} direct · {node.count} including descendants</small></span></label>)}</div>
        {nodes.length === 0 && !busy ? <p>No namespace assignments yet.</p> : null}
      </div> : null}
      {action && !(selectedTaskIds && applied) ? <form className="wb-task-namespace-action" onSubmit={(event) => { event.preventDefault(); void getPreview(); }}>
        {!selectedTaskIds ? <Button type="button" size="small" variant="ghost" onClick={() => setAction(null)}>Back to namespace selection</Button> : null}<h3>{labels[action]}</h3>{sources.length ? <p>Selected: {sources.join(", ")}</p> : null}
        {action === "rename" ? <label className="wb-task-field"><span>New name</span><input aria-label="New name" value={name} required onChange={(event) => setName(event.target.value)} /><small>Changes this final name segment and preserves descendants.</small></label> : null}
        {action === "move" || action === "merge" ? <MultiSelect purpose="selection" label={action === "move" ? "New parent" : "Existing destination"} single searchable values={destination ? [destination] : action === "move" ? ["__root__"] : []} options={[...(action === "move" ? [{ value: "__root__", label: "Root (no parent)" }] : []), ...destinationOptions.map((node) => ({ value: node.path, label: node.path }))]} onChange={(values) => setDestination(values[0] === "__root__" ? "" : values[0] ?? "")} /> : null}
        {action === "merge" || action === "remove" ? <label className="wb-task-checkbox"><input type="checkbox" checked={includeDescendants} onChange={(event) => setIncludeDescendants(event.target.checked)} /><span>Include descendants {action === "merge" ? "and preserve their suffixes" : "in removed assignments"}</span></label> : null}
        {action === "promote" ? <><p>Move this grouping’s children to its parent, preserving deeper paths.</p>{sourceDirect > 0 ? <fieldset><legend>{sourceDirect} direct assignments need a destination</legend><label className="wb-task-checkbox"><input type="radio" name="parent-assignment" value="keep" checked={parentAssignment === "keep"} onChange={() => setParentAssignment("keep")} /><span>Keep assignments on this namespace</span></label><label className="wb-task-checkbox"><input type="radio" name="parent-assignment" value="move" checked={parentAssignment === "move"} onChange={() => setParentAssignment("move")} /><span>Move direct assignments to another namespace</span></label>{parentAssignment === "move" ? <label className="wb-task-field"><span>Direct assignment destination</span><input value={parentDestination} required onChange={(event) => setParentDestination(event.target.value)} /></label> : null}</fieldset> : null}</> : null}
        {action === "remove" ? <p>Removes namespace assignments. Task records and their project links remain intact.</p> : null}
        {action === "assign" ? <><fieldset><legend>Assignment operation</legend>{["add", "remove", "replace"].map((mode) => <label className="wb-task-checkbox" key={mode}><input type="radio" name="assignment-mode" checked={assignmentMode === mode} onChange={() => setAssignmentMode(mode)} /><span>{mode[0]!.toUpperCase() + mode.slice(1)} namespaces</span></label>)}</fieldset><MultiSelect purpose="selection" label="Namespaces" values={assignments} options={nodes.map((node) => ({ value: node.path, label: node.path }))} searchable onChange={setAssignments} /><div className="wb-task-active-filters">{assignments.map((namespace) => <Button key={namespace} size="small" aria-label={`Remove ${namespace} from assignment change`} onClick={() => setAssignments(toggleValue(assignments, namespace))}>{namespace} ×</Button>)}</div>{assignmentMode !== "remove" ? <div className="wb-task-action-create"><label className="wb-task-field wb-task-field--grow"><span>New namespace</span><input value={newNamespace} placeholder="Optional new path" onChange={(event) => setNewNamespace(event.target.value)} /></label><Button type="button" disabled={!newNamespace.trim()} onClick={() => { setAssignments([...new Set([...assignments, newNamespace.trim()])]); setNewNamespace(""); }}>Add path</Button></div> : null}{assignmentMode === "replace" && assignments.length === 0 ? <InlineAlert tone="warning">Replacing with no namespaces removes all namespace assignments from these tasks.</InlineAlert> : null}</> : null}
        <div className="wb-task-actions"><Button type="button" disabled={busy} variant="ghost" onClick={() => selectedTaskIds ? onClose() : setAction(null)}>Cancel</Button><Button type="submit" variant="primary" disabled={busy || readOnly || (action === "merge" && !destination) || (action === "promote" && sourceDirect > 0 && !parentAssignment)}>{busy ? "Loading…" : "Preview change"}</Button></div>
      </form> : null}
      {!selectedTaskIds && operations.length ? <details className="wb-task-namespace-history"><summary>Recent namespace changes</summary><ul>{operations.map((operation) => <li key={operation.operation_id}><span>{operation.label} · <time dateTime={operation.created_at}>{new Date(operation.created_at).toLocaleString()}</time>{operation.undone_at ? " · Undone" : ""}</span>{operation.can_undo ? <Button help={TASK_HELP.undoNamespaces} size="small" disabled={busy || readOnly} onClick={() => void undo(operation)}>Undo {operation.label.toLowerCase()}</Button> : null}</li>)}</ul></details> : null}
      {message?.tone === "danger" && !action ? <Button disabled={busy} onClick={() => void inventory().catch((error: Error) => setMessage({ tone: "danger", text: error.message }))}>Retry loading namespaces</Button> : null}
    </>}
  </section>;
}
