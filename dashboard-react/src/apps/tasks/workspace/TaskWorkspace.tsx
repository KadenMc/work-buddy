import { ArrowDown, ArrowUp, MagnifyingGlass } from "@phosphor-icons/react";
import { type RefObject, useEffect, useMemo, useRef, useState } from "react";
import type { IntentResult, JsonValue, WidgetIntent, WidgetRendererProps } from "../../../dashboard/contributions/contracts";
import { useDashboardAnnouncer } from "../../../dashboard/accessibility/DashboardAnnouncer";
import { ActionMenu, InlineAlert } from "../../../ui";
import { TaskButton as Button, TaskHelp, TASK_HELP } from "./TaskHelp";
import { createCorrelationId, createWidgetIntent } from "../../../widget-library/shared";
import { TASK_INTENTS, type TaskQueryState, type TaskSort, type TaskSummary, type TaskWorkspaceInput } from "../contracts";
import { TaskDetail } from "./TaskDetail";
import { TaskList } from "./TaskList";
import { TaskProposalDetail } from "./TaskProposalDetail";
import { ATTENTION_OPTIONS, STATUS_OPTIONS, FilterPill, MultiSelect, NamespaceRail, toggleValue } from "./TaskFilters";
import { NamespaceOrganizer } from "./NamespaceOrganizer";
import { TaskCompletionDialog } from "./TaskCompletionDialog";

export const tomorrow = (current = new Date()): string => {
  const date = new Date(current.getTime()); date.setDate(date.getDate() + 1);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
};
const SORTS = [{ value: "created_at", label: "Date created" }, { value: "updated_at", label: "Date updated" }, { value: "title", label: "Title" }, { value: "due_date", label: "Due date" }, { value: "urgency", label: "Urgency" }] as const;
const defaults: Record<TaskSort, "asc" | "desc"> = { created_at: "desc", updated_at: "desc", title: "asc", due_date: "asc", urgency: "desc" };
const dateOptions = [{ value: "overdue", label: "Overdue" }, { value: "today", label: "Due today" }, { value: "week", label: "Next 7 days" }, { value: "none", label: "No due date" }];
const urgencyOptions = ["high", "medium", "low"].map((value) => ({ value, label: value[0]!.toUpperCase() + value.slice(1) }));
const knowledgeOptions = [{ value: "yes", label: "Has document" }, { value: "no", label: "No document" }];
type FilterKey = "statuses" | "projects" | "namespaces" | "exact_namespaces" | "attention" | "urgencies";
const queryValues = (query: TaskQueryState, key: FilterKey): readonly string[] => query[key] ?? (key === "statuses" ? ["open"] : []);

export default function TaskWorkspace({ input, emit, presentation }: WidgetRendererProps<TaskWorkspaceInput>) {
  const { announce } = useDashboardAnnouncer();
  const [search, setSearch] = useState(input.query.q);
  const [optimistic, setOptimistic] = useState<TaskQueryState>(input.query);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: "danger" | "success" | "warning"; text: string } | null>(null);
  const [namespaceVisible, setNamespaceVisible] = useState(() => {
    const narrow = presentation.width > 0 && presentation.width < 768 || typeof matchMedia === "function" && matchMedia("(max-width: 767px)").matches;
    try { const saved = localStorage.getItem(narrow ? "wb.tasks.namespace-visible-mobile" : "wb.tasks.namespace-visible"); return saved === null ? !narrow : saved === "true"; } catch { return !narrow; }
  });
  const narrowHost = presentation.width > 0 ? presentation.width < 768 : typeof matchMedia === "function" && matchMedia("(max-width: 767px)").matches;
  useEffect(() => {
    try { const saved = localStorage.getItem(narrowHost ? "wb.tasks.namespace-visible-mobile" : "wb.tasks.namespace-visible"); setNamespaceVisible(saved === null ? !narrowHost : saved === "true"); } catch { setNamespaceVisible(!narrowHost); }
  }, [narrowHost]);
  const [selected, setSelected] = useState<readonly string[]>([]);
  const [bulkOpen, setBulkOpen] = useState(false);
  const [completionTask, setCompletionTask] = useState<TaskSummary | null>(null);
  const [pendingDeleteUndo, setPendingDeleteUndo] = useState<{ taskId: string; revision: number } | null>(null);
  const [triageOrder, setTriageOrder] = useState<readonly string[]>([]);
  const taskRefs = useRef(new Map<string, HTMLButtonElement>());
  const browseRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const backOrigin = useRef<string | null>(input.query.task);
  const browseScroll = useRef({ window: 0, list: 0, frame: 0 });
  const frameRef = useRef<HTMLElement | null>(null);
  const hadSelection = useRef(Boolean(input.query.task || input.query.proposal));
  const navigationGeneration = useRef(0);
  const directionMemory = useRef<Partial<Record<TaskSort, "asc" | "desc">>>({});
  const latest = useRef(input.query);
  const query = optimistic;
  const hasSelection = Boolean(input.query.task || input.query.proposal);
  const triage = query.mode === "triage";
  const organizer = query.mode === "namespaces";
  const readOnly = input.access.mode === "read_only";
  const sort = query.sort ?? "created_at";
  const direction = query.direction ?? defaults[sort];
  const orderLabel = sort === "title" ? direction === "asc" ? "A–Z" : "Z–A" : sort === "urgency" ? direction === "desc" ? "Highest first" : "Lowest first" : sort === "due_date" ? direction === "asc" ? "Earliest first" : "Latest first" : direction === "desc" ? "Newest first" : "Oldest first";
  const projects = useMemo(() => [{ value: "__none__", label: "No project" }, ...input.options.projects.filter((option) => option.value !== "__none__")], [input.options.projects]);

  useEffect(() => { setOptimistic(input.query); latest.current = input.query; setSearch(input.query.q); setRefreshing(false); }, [input.query]);
  useEffect(() => {
    if (!hasSelection && hadSelection.current) {
      requestAnimationFrame(() => {
        if (browseRef.current) browseRef.current.scrollTop = browseScroll.current.list;
        if (frameRef.current) frameRef.current.scrollTop = browseScroll.current.frame;
        if (window.scrollY !== browseScroll.current.window) window.scrollTo?.({ top: browseScroll.current.window });
        const origin = backOrigin.current ? taskRefs.current.get(backOrigin.current) : undefined;
        (origin ?? searchInputRef.current)?.focus({ preventScroll: true });
      });
    }
    hadSelection.current = hasSelection;
  }, [hasSelection]);
  useEffect(() => {
    const incoming = input.tasks.map((task) => task.task_id);
    setTriageOrder((current) => [...current.filter((id) => incoming.includes(id)), ...incoming.filter((id) => !current.includes(id))]);
  }, [input.tasks]);
  const orderedTasks = useMemo(() => triage ? triageOrder.flatMap((id) => { const task = input.tasks.find((item) => item.task_id === id); return task ? [task] : []; }) : input.tasks, [input.tasks, triage, triageOrder]);

  const send = async (type: string, payload: JsonValue, mutation = false, reuseId?: string): Promise<IntentResult> => {
    const id = reuseId ?? createCorrelationId(type.split(".").slice(-1)[0] ?? "tasks");
    return emit(createWidgetIntent(presentation, type, payload, { intentId: id, ...(mutation ? { clientMutationId: id } : {}) }) as WidgetIntent);
  };
  const navigate = (patch: Record<string, JsonValue>, replace = true) => {
    const generation = ++navigationGeneration.current;
    const next = { ...latest.current, ...patch } as TaskQueryState;
    latest.current = next; setOptimistic(next); setRefreshing(true); setRefreshError(null);
    void send(TASK_INTENTS.locationChange, { patch, replace }).then((result) => {
      if (generation !== navigationGeneration.current) return;
      if (result.status !== "accepted") { setRefreshError(result.message ?? "Tasks could not refresh."); setRefreshing(false); }
    }).catch((error: unknown) => {
      if (generation !== navigationGeneration.current) return;
      const text = error instanceof Error ? error.message : "Task view could not change.";
      setRefreshError(text); setRefreshing(false); announce(text, "assertive");
    });
  };
  const filter = (patch: Record<string, JsonValue>) => navigate({ ...patch, offset: 0 });
  useEffect(() => {
    if (search === latest.current.q) return;
    const timer = setTimeout(() => filter({ q: search }), 250);
    return () => clearTimeout(timer);
  }, [search]);
  const select = (taskId: string) => {
    backOrigin.current = taskId;
    frameRef.current = browseRef.current?.closest<HTMLElement>(".wb-widget-frame__content") ?? null;
    browseScroll.current = { window: window.scrollY, list: browseRef.current?.scrollTop ?? 0, frame: frameRef.current?.scrollTop ?? 0 };
    navigate({ task: taskId, proposal: null }, false);
  };
  const closeDetails = () => navigate({ task: null, proposal: null }, false);
  const setRail = (visible: boolean) => { setNamespaceVisible(visible); try { localStorage.setItem(narrowHost ? "wb.tasks.namespace-visible-mobile" : "wb.tasks.namespace-visible", String(visible)); } catch { /* Optional preference. */ } };
  const clearFilters = () => { setSearch(""); filter({ q: "", statuses: [], projects: [], namespaces: [], exact_namespaces: [], attention: [], urgencies: [], due: "", note: "", mode: "browse" }); };
  const toggleTriage = () => filter(triage ? { mode: "browse" } : { mode: "triage", statuses: ["open"], attention: ["inbox"] });
  const runSummaryAction = async (task: TaskSummary, action: "complete" | "reopen" | "focus" | "mit" | "snooze" | "archive", reuseId?: string): Promise<IntentResult> => {
    if (readOnly) return { intent_id: reuseId ?? "read-only", status: "unavailable", message: "Task editing is unavailable." };
    const type = { complete: TASK_INTENTS.complete, reopen: TASK_INTENTS.reopen, focus: TASK_INTENTS.focus, mit: TASK_INTENTS.update, snooze: TASK_INTENTS.snooze, archive: TASK_INTENTS.archive }[action];
    const result = await send(type, { task_id: task.task_id, expected_revision: task.revision, ...(action === "mit" ? { attention_state: "mit" } : {}), ...(action === "snooze" ? { snooze_until: tomorrow() } : {}) }, true, reuseId).catch((error: unknown): IntentResult => ({ intent_id: "failed", status: "unavailable", message: error instanceof Error ? error.message : "Task action unavailable." }));
    const text = result.message ?? (result.status === "accepted" ? "Task updated." : "Task could not be updated.");
    setNotice({ tone: result.status === "accepted" ? "success" : result.status === "conflict" ? "warning" : "danger", text }); announce(text, result.status === "accepted" ? "polite" : "assertive");
    if (result.status === "conflict") navigate({});
    return result;
  };
  const skip = (task: TaskSummary) => { const next = [...triageOrder.filter((id) => id !== task.task_id), task.task_id]; setTriageOrder(next); announce(`${task.title} moved to the end of this triage pass.`); requestAnimationFrame(() => { if (next[0]) taskRefs.current.get(next[0])?.focus({ preventScroll: true }); }); };
  const namespaceNodes = input.namespace_tree?.length ? input.namespace_tree : input.options.namespaces.map((option) => ({ path: option.value, parent: option.value.includes("/") ? option.value.slice(0, option.value.lastIndexOf("/")) : null, label: option.label, count: input.facets.namespaces[option.value] ?? 0, direct_count: input.facets.namespaces[option.value] ?? 0 }));
  const resultCount = input.total ?? input.tasks.length;
  const error = refreshError ?? input.refresh_error;

  return <section className="wb-task-workspace wb-task-workspace--redesigned" data-layout={presentation.width > 0 && presentation.width < 768 ? "stacked" : "wide"} aria-label="Task workspace">
    {completionTask ? <TaskCompletionDialog task={completionTask} onClose={() => setCompletionTask(null)} onConfirm={(id) => runSummaryAction(completionTask, "complete", id)} /> : null}
    {notice ? <InlineAlert tone={notice.tone}>{notice.text}</InlineAlert> : null}
    {hasSelection && error ? <InlineAlert tone="danger">{error} <Button size="small" onClick={() => navigate({})}>Retry</Button></InlineAlert> : null}
    {hasSelection ? <section className="wb-task-dedicated-view" aria-label="Task details">
      {input.selectedProposal ? <TaskProposalDetail key={input.query.proposal} selection={input.selectedProposal} options={input.options} readOnly={readOnly} presentation={presentation} emit={emit} onClose={closeDetails} /> : input.selectedTask ? <TaskDetail key={input.selectedTask.task_id} task={input.selectedTask} options={input.options} readOnly={readOnly} presentation={presentation} emit={emit} onClose={closeDetails} undoDeleteRevision={pendingDeleteUndo?.taskId === input.selectedTask.task_id ? pendingDeleteUndo.revision : null} onDeleteAcknowledged={(revision) => setPendingDeleteUndo({ taskId: input.selectedTask!.task_id, revision })} onDeleteUndone={() => setPendingDeleteUndo(null)} /> : <div className="wb-task-empty"><Button onClick={closeDetails}>Back to tasks</Button><p>{refreshing ? "Opening task…" : "This task is unavailable. Return to your task list to continue browsing."}</p></div>}
    </section> : organizer || bulkOpen ? <NamespaceOrganizer send={send} readOnly={readOnly} selectedTaskIds={bulkOpen ? selected : undefined} onClose={() => { setBulkOpen(false); if (organizer) navigate({ mode: "browse" }); }} onApplied={() => { setSelected([]); }} /> : <>
      <div className="wb-task-browse-tools">
        <form className="wb-task-search" role="search" onSubmit={(event) => { event.preventDefault(); filter({ q: search }); }}><label><span>Search tasks</span><span className="wb-task-search__control"><MagnifyingGlass aria-hidden="true" /><input ref={searchInputRef} type="search" value={search} placeholder="Find a task…" onChange={(event) => setSearch(event.target.value)} /></span></label></form>
        {narrowHost ? <div className="wb-task-browse-actions wb-task-browse-actions--compact"><TaskHelp content={{ summary: "Show namespace controls and Inbox triage.", details: "Show namespaces reveals the filtering panel. Manage namespaces opens a preview-based organizer. Triage inbox visibly selects Open status and Inbox attention; opening this menu does not change tasks." }} focusable><span>Actions</span></TaskHelp><ActionMenu label="Task actions" sections={[{ id: "task-browsing", items: [
          { id: "namespaces", label: namespaceVisible ? "Hide namespaces" : "Show namespaces", onAction: () => setRail(!namespaceVisible) },
          { id: "manage-namespaces", label: "Manage namespaces", onAction: () => navigate({ mode: "namespaces" }) },
          { id: "triage", label: triage ? "Finish triage" : "Triage inbox", onAction: toggleTriage },
        ] }]} /></div> : <div className="wb-task-browse-actions">{!namespaceVisible ? <Button size="small" variant="ghost" onClick={() => setRail(true)}>Show namespaces</Button> : null}<Button size="small" variant="ghost" onClick={() => navigate({ mode: "namespaces" })}>Manage namespaces</Button><Button size="small" variant={triage ? "primary" : "ghost"} onClick={toggleTriage}>{triage ? "Finish triage" : "Triage inbox"}</Button></div>}
      </div>
      <div className="wb-task-filter-toolbar" aria-label="Task filters">
        <MultiSelect label="Status" values={queryValues(query, "statuses")} options={STATUS_OPTIONS} counts={input.facets.statuses} onChange={(statuses) => filter({ statuses })} />
        <MultiSelect label="Projects" values={queryValues(query, "projects")} options={projects} searchable counts={input.facets.projects} onChange={(projects) => filter({ projects })} />
        <MultiSelect label="Attention" values={queryValues(query, "attention")} options={ATTENTION_OPTIONS} counts={input.facets.attention} onChange={(attention) => filter({ attention })} />
        <MultiSelect label="Urgency" values={queryValues(query, "urgencies")} options={urgencyOptions} counts={input.facets.urgencies} onChange={(urgencies) => filter({ urgencies })} />
        <MultiSelect label="Due date" single values={query.due ? [query.due] : []} options={dateOptions} onChange={(due) => filter({ due: due[0] ?? "" })} />
        <MultiSelect label="Knowledge" single values={query.note ? [query.note] : []} options={knowledgeOptions} onChange={(note) => filter({ note: note[0] ?? "" })} />
      </div>
      <div className="wb-task-active-filters" aria-label="Selected filters">
        {(["statuses", "projects", "attention", "urgencies", "namespaces", "exact_namespaces"] as const).flatMap((key) => queryValues(query, key).map((value) => {
          const options = key === "statuses" ? STATUS_OPTIONS : key === "projects" ? projects : key === "attention" ? ATTENTION_OPTIONS : urgencyOptions;
          const text = value === "__none__" ? key === "projects" ? "No project" : "No namespace" : options.find((option) => option.value === value)?.label ?? value;
          const label = key === "namespaces" && value !== "__none__" ? `${text} + descendants` : key === "exact_namespaces" ? `${text} only` : text;
          return <FilterPill key={`${key}:${value}`} label={label} onRemove={() => filter({ [key]: toggleValue(queryValues(query, key), value) })} />;
        }))}
        {query.due ? <FilterPill label={dateOptions.find((option) => option.value === query.due)?.label ?? query.due} onRemove={() => filter({ due: "" })} /> : null}
        {query.note ? <FilterPill label={knowledgeOptions.find((option) => option.value === query.note)?.label ?? query.note} onRemove={() => filter({ note: "" })} /> : null}
        {search ? <FilterPill label={`Search: ${search}`} onRemove={() => { setSearch(""); filter({ q: "" }); }} /> : null}
        <Button size="small" variant="ghost" onClick={clearFilters}>Clear filters</Button>
      </div>
      {triage ? <InlineAlert tone="info">Inbox triage · choose how to handle each task, or skip it for this pass. The visible filters control this list.</InlineAlert> : null}
      <div className={`wb-task-browse-grid${namespaceVisible ? " has-namespaces" : ""}`}>
        {namespaceVisible ? <NamespaceRail noNamespaceCount={input.facets.namespaces.__none__ ?? 0} nodes={namespaceNodes} selected={queryValues(query, "namespaces")} exact={queryValues(query, "exact_namespaces")} onChange={(namespaces, exact_namespaces) => filter({ namespaces: [...namespaces], exact_namespaces: [...exact_namespaces] })} onHide={() => setRail(false)} onManage={() => navigate({ mode: "namespaces" })} /> : null}
        <div className="wb-task-results" ref={browseRef}>
          <div className="wb-task-results-bar"><div><h2 className={triage ? undefined : "wb-task-sr-only"}>{triage ? "Inbox triage" : "Tasks"}</h2><p role="status">{refreshing || input.refreshing ? "Updating… " : ""}{resultCount} {resultCount === 1 ? "task" : "tasks"}</p></div>
            <div className="wb-task-sort" role="group" aria-label="Task sorting"><label><span>Sort by</span><TaskHelp content={TASK_HELP.sort}><select aria-label="Sort by" value={sort} onChange={(event) => { directionMemory.current[sort] = direction; const next = event.target.value as TaskSort; filter({ sort: next, direction: directionMemory.current[next] ?? defaults[next] }); }}>{SORTS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></TaskHelp></label><Button help={TASK_HELP.sortDirection} size="small" variant="ghost" title={`Reverse sort: ${orderLabel}`} aria-label={`Reverse sort direction, currently ${orderLabel}`} onClick={() => { const next = direction === "asc" ? "desc" : "asc"; directionMemory.current[sort] = next; filter({ direction: next }); }}>{direction === "desc" ? <ArrowDown aria-hidden="true" /> : <ArrowUp aria-hidden="true" />}<span>{orderLabel}</span></Button></div>
          </div>
          {error ? <InlineAlert tone="danger">{error} Your previous results remain visible. <Button size="small" onClick={() => navigate({})}>Retry</Button></InlineAlert> : null}
          {selected.length ? <div className="wb-task-selection-bar"><strong>{selected.length} selected</strong><Button size="small" disabled={readOnly} onClick={() => setBulkOpen(true)}>Change namespaces</Button><Button size="small" variant="ghost" onClick={() => setSelected([])}>Clear selection</Button></div> : null}
          <TaskList tasks={orderedTasks} selectedTaskId={null} selectedTaskIds={selected} onToggleSelection={(id) => setSelected((current) => toggleValue(current, id))} projects={input.options.projects} sort={sort} triage={triage} readOnly={readOnly} focusRefs={taskRefs as RefObject<Map<string, HTMLButtonElement>>} onSelect={select} onAction={(task, action) => { if (action === "complete") setCompletionTask(task); else void runSummaryAction(task, action); }} onSkip={skip} />
          {input.page && (input.page.offset > 0 || input.page.has_more) ? <nav className="wb-task-pagination" aria-label="Task pages"><Button disabled={input.page.offset === 0 || refreshing} onClick={() => navigate({ offset: Math.max(0, input.page!.offset - input.page!.limit) })}>Previous</Button><span>{input.page.offset + 1}–{Math.min(input.page.offset + input.tasks.length, resultCount)} of {resultCount}</span><Button disabled={!input.page.has_more || refreshing} onClick={() => navigate({ offset: input.page!.offset + input.page!.limit })}>Next</Button></nav> : null}
        </div>
      </div>
    </>}
  </section>;
}
