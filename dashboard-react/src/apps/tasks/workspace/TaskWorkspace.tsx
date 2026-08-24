import { Funnel, MagnifyingGlass } from "@phosphor-icons/react";
import {
  type KeyboardEvent,
  type RefObject,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type {
  IntentResult,
  JsonValue,
  WidgetIntent,
  WidgetRendererProps,
} from "../../../dashboard/contributions/contracts";
import { useDashboardAnnouncer } from "../../../dashboard/accessibility/DashboardAnnouncer";
import { Button, InlineAlert } from "../../../ui";
import { createCorrelationId, createWidgetIntent } from "../../../widget-library/shared";
import {
  TASK_INTENTS,
  type TaskLens,
  type TaskSummary,
  type TaskWorkspaceInput,
} from "../contracts";
import { TaskDetail } from "./TaskDetail";
import { TaskList } from "./TaskList";

const LENSES: readonly { readonly value: TaskLens; readonly label: string }[] = [
  { value: "focused", label: "Focused" },
  { value: "inbox", label: "Inbox" },
  { value: "active", label: "All active" },
  { value: "snoozed", label: "Snoozed" },
  { value: "completed", label: "Completed / Archived" },
  { value: "trash", label: "Trash" },
  { value: "triage", label: "Inbox triage" },
];

interface FilterValues {
  readonly project: string;
  readonly namespace: string;
  readonly urgency: string;
  readonly due: string;
  readonly state: string;
  readonly note: string;
}

export const tomorrow = (current = new Date()): string => {
  const date = new Date(current.getTime());
  date.setDate(date.getDate() + 1);
  const year = String(date.getFullYear()).padStart(4, "0");
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
};

const useMobileLayout = (): boolean => {
  const [mobile, setMobile] = useState(
    () => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(max-width: 767px)").matches,
  );
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(max-width: 767px)");
    const update = () => setMobile(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return mobile;
};

interface FilterControlsProps {
  readonly filters: FilterValues;
  readonly projects: readonly { readonly value: string; readonly label: string }[];
  readonly namespaces: readonly { readonly value: string; readonly label: string }[];
  onChange(next: FilterValues): void;
}

function FilterControls({ filters, projects, namespaces, onChange }: FilterControlsProps) {
  const update = (key: keyof FilterValues, value: string) => onChange({ ...filters, [key]: value });
  return (
    <div className="wb-task-filters__fields">
      <label className="wb-task-field"><span>Project</span><select value={filters.project} onChange={(event) => update("project", event.target.value)}><option value="">Any project</option>{projects.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label>
      <label className="wb-task-field"><span>Namespace</span><select value={filters.namespace} onChange={(event) => update("namespace", event.target.value)}><option value="">Any namespace</option>{namespaces.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label>
      <label className="wb-task-field"><span>Urgency</span><select value={filters.urgency} onChange={(event) => update("urgency", event.target.value)}><option value="">Any urgency</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option></select></label>
      <label className="wb-task-field"><span>Date</span><select value={filters.due} onChange={(event) => update("due", event.target.value)}><option value="">Any date</option><option value="overdue">Overdue</option><option value="today">Due today</option><option value="week">Next 7 days</option><option value="none">No due date</option></select></label>
      <label className="wb-task-field"><span>State</span><select value={filters.state} onChange={(event) => update("state", event.target.value)}><option value="">Any state</option><option value="inbox">Inbox</option><option value="mit">Most Important</option><option value="active">Active</option><option value="focused">Focused</option><option value="waiting">Waiting</option></select></label>
      <label className="wb-task-field"><span>Knowledge</span><select value={filters.note} onChange={(event) => update("note", event.target.value)}><option value="">With or without</option><option value="yes">Has document</option><option value="no">No document</option></select></label>
    </div>
  );
}

export default function TaskWorkspace({
  input,
  emit,
  presentation,
}: WidgetRendererProps<TaskWorkspaceInput>) {
  const { announce } = useDashboardAnnouncer();
  const [search, setSearch] = useState(input.query.q);
  const [filters, setFilters] = useState<FilterValues>({
    project: input.query.project,
    namespace: input.query.namespace,
    urgency: input.query.urgency,
    due: input.query.due,
    state: input.query.state,
    note: input.query.note,
  });
  const [filterOpen, setFilterOpen] = useState(false);
  const [mobilePane, setMobilePane] = useState<"list" | "details">(
    input.selectedTask === null ? "list" : "details",
  );
  const [notice, setNotice] = useState<{ tone: "danger" | "success" | "warning"; text: string } | null>(null);
  const [pendingDeleteUndo, setPendingDeleteUndo] = useState<{
    readonly taskId: string;
    readonly revision: number;
  } | null>(null);
  const [triageOrder, setTriageOrder] = useState<readonly string[]>(
    () => input.tasks.map((task) => task.task_id),
  );
  const filterDialogRef = useRef<HTMLDivElement>(null);
  const filterTriggerRef = useRef<HTMLButtonElement>(null);
  const mobileListTabRef = useRef<HTMLButtonElement>(null);
  const mobileDetailTabRef = useRef<HTMLButtonElement>(null);
  const taskRefs = useRef(new Map<string, HTMLButtonElement>());
  const selectedOriginRef = useRef<string | null>(input.query.task);
  const readOnly = input.access.mode === "read_only";
  const mobile = useMobileLayout();

  useEffect(() => setSearch(input.query.q), [input.query.q]);
  useEffect(() => {
    if (input.selectedTask === null && mobilePane === "details") setMobilePane("list");
  }, [input.selectedTask, mobilePane]);
  useEffect(() => {
    setFilters({
      project: input.query.project,
      namespace: input.query.namespace,
      urgency: input.query.urgency,
      due: input.query.due,
      state: input.query.state,
      note: input.query.note,
    });
  }, [input.query.due, input.query.namespace, input.query.note, input.query.project, input.query.state, input.query.urgency]);
  useEffect(() => {
    const incoming = input.tasks.map((task) => task.task_id);
    const incomingSet = new Set(incoming);
    setTriageOrder((current) => {
      const retained = current.filter((taskId) => incomingSet.has(taskId));
      const retainedSet = new Set(retained);
      const next = [...retained, ...incoming.filter((taskId) => !retainedSet.has(taskId))];
      return next.length === current.length && next.every((taskId, index) => taskId === current[index])
        ? current
        : next;
    });
  }, [input.tasks]);

  const orderedTasks = useMemo(() => {
    if (input.query.lens !== "triage") return input.tasks;
    const byId = new Map(input.tasks.map((task) => [task.task_id, task]));
    return triageOrder.flatMap((taskId) => {
      const task = byId.get(taskId);
      return task === undefined ? [] : [task];
    });
  }, [input.query.lens, input.tasks, triageOrder]);

  const send = async (
    type: string,
    payload: JsonValue,
    mutation = false,
  ): Promise<IntentResult> => {
    const parts = type.split(".");
    const id = createCorrelationId(parts[parts.length - 1] ?? "tasks");
    const intent = createWidgetIntent(presentation, type, payload, {
      intentId: id,
      ...(mutation ? { clientMutationId: id } : {}),
    }) as WidgetIntent;
    return emit(intent);
  };

  const navigate = (patch: Record<string, JsonValue>, replace = false) => {
    void send(TASK_INTENTS.locationChange, { patch, replace }).catch((error: unknown) => {
      const text = error instanceof Error ? error.message : "Task view could not change.";
      setNotice({ tone: "danger", text });
      announce(text, "assertive");
    });
  };

  const select = (taskId: string) => {
    selectedOriginRef.current = taskId;
    setMobilePane("details");
    navigate({ task: taskId });
  };

  const closeDetails = () => {
    const origin = selectedOriginRef.current;
    setMobilePane("list");
    navigate({ task: null });
    window.requestAnimationFrame(() => {
      if (origin) taskRefs.current.get(origin)?.focus({ preventScroll: true });
    });
  };

  const runSummaryAction = async (
    task: TaskSummary,
    action: "complete" | "reopen" | "focus" | "mit" | "snooze" | "archive",
  ) => {
    if (readOnly) return;
    const type = {
      complete: TASK_INTENTS.complete,
      reopen: TASK_INTENTS.reopen,
      focus: TASK_INTENTS.focus,
      mit: TASK_INTENTS.update,
      snooze: TASK_INTENTS.snooze,
      archive: TASK_INTENTS.archive,
    }[action];
    const result = await send(type, {
      task_id: task.task_id,
      expected_revision: task.revision,
      ...(action === "mit" ? { attention_state: "mit" } : {}),
      ...(action === "snooze" ? { snooze_until: tomorrow() } : {}),
    }, true).catch((error: unknown): IntentResult => ({
      intent_id: "failed",
      status: "unavailable",
      message: error instanceof Error ? error.message : "Task action is unavailable.",
    }));
    const text = result.message ?? (result.status === "accepted" ? "Task updated." : "Task could not be updated.");
    setNotice({ tone: result.status === "accepted" ? "success" : result.status === "conflict" ? "warning" : "danger", text });
    announce(text, result.status === "accepted" ? "polite" : "assertive");
    if (result.status === "conflict") {
      await send(TASK_INTENTS.locationChange, {
        patch: { ...(input.query.task === null ? {} : { task: input.query.task }) },
        replace: true,
      });
    }
  };

  const skipTriageTask = (task: TaskSummary) => {
    const incoming = input.tasks.map((item) => item.task_id);
    const incomingSet = new Set(incoming);
    const retained = triageOrder.filter((taskId) => incomingSet.has(taskId));
    const retainedSet = new Set(retained);
    const current = [...retained, ...incoming.filter((taskId) => !retainedSet.has(taskId))];
    const index = current.indexOf(task.task_id);
    if (index < 0) return;
    const next = [...current.slice(0, index), ...current.slice(index + 1), task.task_id];
    const focusId = next[Math.min(index, next.length - 1)] ?? null;
    setTriageOrder(next);
    const message = `${task.title} moved to the end of this triage pass.`;
    announce(message, "polite");
    window.requestAnimationFrame(() => {
      if (focusId) taskRefs.current.get(focusId)?.focus({ preventScroll: true });
    });
  };

  const applyFilters = () => {
    navigate({ ...filters });
    setFilterOpen(false);
    filterTriggerRef.current?.focus({ preventScroll: true });
  };

  const trapFilterFocus = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      setFilterOpen(false);
      filterTriggerRef.current?.focus({ preventScroll: true });
      return;
    }
    if (event.key !== "Tab") return;
    const controls = [...(filterDialogRef.current?.querySelectorAll<HTMLElement>(
      "button:not([disabled]), input:not([disabled]), select:not([disabled])",
    ) ?? [])];
    if (controls.length === 0) return;
    const first = controls[0]!;
    const last = controls[controls.length - 1]!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const navigateMobileTabs = (
    event: KeyboardEvent<HTMLButtonElement>,
    current: "list" | "details",
  ) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const panes: readonly ("list" | "details")[] = input.selectedTask === null
      ? ["list"]
      : ["list", "details"];
    const currentIndex = panes.indexOf(current);
    const next = event.key === "Home"
      ? panes[0]!
      : event.key === "End"
        ? panes[panes.length - 1]!
        : panes[
            (currentIndex + (event.key === "ArrowRight" ? 1 : -1) + panes.length) % panes.length
          ]!;
    setMobilePane(next);
    (next === "list" ? mobileListTabRef : mobileDetailTabRef).current?.focus();
  };

  return (
    <section className="wb-task-workspace" aria-label="Task workspace">
      {input.access.mode === "read_only" ? <InlineAlert tone="warning">{input.access.reason ?? "Tasks is read-only."}</InlineAlert> : null}
      {notice ? <InlineAlert tone={notice.tone}>{notice.text}</InlineAlert> : null}

      <nav className="wb-task-lenses" aria-label="Task lenses">
        {LENSES.map((lens) => (
          <Button
            key={lens.value}
            size="small"
            variant={input.query.lens === lens.value ? "primary" : "ghost"}
            aria-current={input.query.lens === lens.value ? "page" : undefined}
            onClick={() => navigate({ lens: lens.value, task: null })}
          >
            {lens.label} <span className="wb-task-lens-count">{input.facets.counts[lens.value]}</span>
          </Button>
        ))}
      </nav>

      <div className="wb-task-workspace__toolbar">
        <form className="wb-task-search" role="search" onSubmit={(event) => { event.preventDefault(); navigate({ q: search, task: null }); }}>
          <label><span>Search tasks</span><span className="wb-task-search__control"><MagnifyingGlass aria-hidden="true" /><input type="search" value={search} placeholder="Title, tag, project…" onChange={(event) => setSearch(event.target.value)} /><Button type="submit" size="small">Search</Button></span></label>
        </form>
        <Button ref={filterTriggerRef} className="wb-task-filter-trigger" aria-expanded={filterOpen} onClick={() => setFilterOpen(true)}><Funnel aria-hidden="true" /> Filters</Button>
      </div>

      <div className="wb-task-workspace__mobile-tabs" role="tablist" aria-label="Task workspace panes">
        <button ref={mobileListTabRef} id="wb-task-list-tab" type="button" role="tab" tabIndex={mobilePane === "list" ? 0 : -1} aria-selected={mobilePane === "list"} aria-controls="wb-task-list-panel" onKeyDown={(event) => navigateMobileTabs(event, "list")} onClick={() => setMobilePane("list")}>List</button>
        <button ref={mobileDetailTabRef} id="wb-task-detail-tab" type="button" role="tab" tabIndex={mobilePane === "details" ? 0 : -1} aria-selected={mobilePane === "details"} aria-controls="wb-task-detail-panel" disabled={input.selectedTask === null} onKeyDown={(event) => navigateMobileTabs(event, "details")} onClick={() => setMobilePane("details")}>Details</button>
      </div>

      <div className="wb-task-workspace__body">
        <aside className="wb-task-filters" aria-label="Task filters">
          <h2>Filters</h2>
          <FilterControls filters={filters} projects={input.options.projects} namespaces={input.options.namespaces} onChange={setFilters} />
          <Button size="small" onClick={applyFilters}>Apply filters</Button>
          <Button size="small" variant="ghost" onClick={() => { const empty = { project: "", namespace: "", urgency: "", due: "", state: "", note: "" }; setFilters(empty); navigate(empty); }}>Clear</Button>
        </aside>

        <section id="wb-task-list-panel" className="wb-task-list-pane" role="tabpanel" aria-labelledby="wb-task-list-tab" hidden={mobile && mobilePane !== "list"} inert={mobile && mobilePane !== "list" ? true : undefined}>
          <div className="wb-task-list-pane__heading"><h2>{LENSES.find((lens) => lens.value === input.query.lens)?.label}</h2><span>{input.tasks.length} tasks</span></div>
          <TaskList tasks={orderedTasks} selectedTaskId={input.selectedTask?.task_id ?? null} triage={input.query.lens === "triage"} readOnly={readOnly} focusRefs={taskRefs as RefObject<Map<string, HTMLButtonElement>>} onSelect={select} onAction={(task, next) => void runSummaryAction(task, next)} onSkip={skipTriageTask} />
        </section>

        <section id="wb-task-detail-panel" className="wb-task-detail-pane" role="tabpanel" aria-labelledby="wb-task-detail-tab" hidden={mobile && mobilePane !== "details"} inert={mobile && mobilePane !== "details" ? true : undefined}>
          {input.selectedTask === null ? <div className="wb-task-empty"><p>Select a task to see and edit its details.</p></div> : <TaskDetail key={`${input.selectedTask.task_id}:${input.selectedTask.revision}`} task={input.selectedTask} readOnly={readOnly} presentation={presentation} emit={emit} onClose={closeDetails} undoDeleteRevision={pendingDeleteUndo?.taskId === input.selectedTask.task_id ? pendingDeleteUndo.revision : null} onDeleteAcknowledged={(revision) => setPendingDeleteUndo({ taskId: input.selectedTask!.task_id, revision })} onDeleteUndone={() => setPendingDeleteUndo(null)} />}
        </section>
      </div>

      {filterOpen ? (
        <div className="wb-task-filter-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) { setFilterOpen(false); filterTriggerRef.current?.focus(); } }}>
          <div ref={filterDialogRef} className="wb-task-filter-dialog" role="dialog" aria-modal="true" aria-labelledby="wb-task-filter-dialog-title" onKeyDown={trapFilterFocus}>
            <div className="wb-task-section-heading"><div><h2 id="wb-task-filter-dialog-title">Filter tasks</h2><p>Narrow the current lens.</p></div><Button size="small" variant="ghost" autoFocus onClick={() => { setFilterOpen(false); filterTriggerRef.current?.focus(); }}>Close</Button></div>
            <FilterControls filters={filters} projects={input.options.projects} namespaces={input.options.namespaces} onChange={setFilters} />
            <Button variant="primary" onClick={applyFilters}>Apply filters</Button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
