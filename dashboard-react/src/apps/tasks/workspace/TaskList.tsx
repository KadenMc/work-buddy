import { FileText, Flag, Moon, Trash } from "@phosphor-icons/react";
import type { RefObject } from "react";

import { TaskButton as Button, TASK_HELP } from "./TaskHelp";
import type { IntentResult } from "../../../dashboard/contributions/contracts";
import { TaskNamespacePills } from "./TaskNamespacePills";
import type { TaskProjectOption, TaskSort, TaskSummary } from "../contracts";
import { attentionLabel } from "./TaskFilters";

export interface TaskListProps {
  readonly tasks: readonly TaskSummary[];
  readonly triage: boolean;
  readonly readOnly: boolean;
  readonly focusRefs: RefObject<Map<string, HTMLButtonElement>>;
  readonly namespaceOptions?: readonly TaskProjectOption[];
  readonly projects?: readonly TaskProjectOption[];
  readonly sort?: TaskSort;
  onNamespacesChange?(task: TaskSummary, nextNamespaces: readonly string[], mutationId?: string): Promise<IntentResult>;
  onSelect(taskId: string): void;
  onAction(task: TaskSummary, action: "complete" | "reopen" | "focus" | "mit" | "snooze" | "archive"): void;
  onSkip(task: TaskSummary): void;
}

const dueLabel = (task: TaskSummary): string | null => {
  if (task.deadline_date) return `Deadline ${task.deadline_date}`;
  if (task.due_date) return `Due ${task.due_date}`;
  return null;
};
const validDate = (value: string | null | undefined): Date | null => {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
};

export function TaskList({
  tasks,
  triage,
  readOnly,
  focusRefs,
  onSelect,
  onAction,
  onSkip,
  namespaceOptions = [],
  onNamespacesChange,
  projects = [],
  sort,
}: TaskListProps) {
  const visible = triage ? tasks.slice(0, 5) : tasks;
  if (visible.length === 0) {
    return (
      <div className="wb-task-empty">
        <p>No tasks match this view.</p>
        <p>Adjust the filters or capture something above.</p>
      </div>
    );
  }
  return (
    <ul className="wb-task-list" aria-label="Tasks">
      {visible.map((task) => {
        const completed = task.attention_state === "done" || task.status === "completed" || task.completed_at !== null;
        const historical = Boolean(task.archived_at || task.deleted_at || task.status === "archived" || task.status === "trash");
        const created = validDate(task.created_at);
        const updated = validDate(task.updated_at);
        return (
          <li key={task.task_id}>
            <Button
              help={completed ? TASK_HELP.reopen : TASK_HELP.complete}
              size="small"
              variant="ghost"
              className="wb-task-list__complete"
              aria-label={completed ? `Reopen ${task.title}` : `Complete ${task.title}`}
              disabled={readOnly || historical}
              onClick={() => onAction(task, completed ? "reopen" : "complete")}
            >
              <span aria-hidden="true">{completed ? "↻" : "✓"}</span>
            </Button>
            <div className="wb-task-list__body"><button
              ref={(node) => {
                if (node === null) focusRefs.current?.delete(task.task_id);
                else focusRefs.current?.set(task.task_id, node);
              }}
              type="button"
              className="wb-task-list__select"
              onClick={() => onSelect(task.task_id)}
            >
              <span className="wb-task-list__title">{task.title}</span>
              <span className="wb-task-list__meta">
                <span className={`wb-task-badge wb-task-badge--${task.urgency}`}>
                  <Flag aria-hidden="true" /> {task.urgency}
                </span>
                <span>{attentionLabel(task.attention_state)}</span>
                {(task.project_ids ?? []).map((id) => <span key={id} className="wb-task-row-project">{projects.find((project) => project.value === String(id))?.label ?? `Project ${id}`}</span>)}
                {(task.unresolved_projects ?? []).map((link) => <span key={`${link.legacy_value}:${link.source_tag}`}>Unresolved project: {link.legacy_value}</span>)}
                {dueLabel(task) ? <span>{dueLabel(task)}</span> : null}
                {task.current_action ? <span>Next: {task.current_action}</span> : null}
                {task.has_document ? <span><FileText aria-hidden="true" /> Knowledge</span> : null}
                {task.snooze_until ? <span><Moon aria-hidden="true" /> {task.snooze_until}</span> : null}
                {task.deleted_at ? <span><Trash aria-hidden="true" /> Trash</span> : null}
                {task.archived_at && !task.deleted_at ? <span>Archived</span> : null}
              </span>
              <span className="wb-task-list__dates"><span>Created {created ? <time dateTime={task.created_at ?? undefined} title={created.toLocaleString()}>{created.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}<span className="wb-task-sr-only"> at {created.toLocaleTimeString()}</span></time> : "at an unknown time"}</span>{sort === "updated_at" ? <span>Updated {updated ? <time dateTime={task.updated_at} title={updated.toLocaleString()}>{updated.toLocaleDateString()}</time> : "at an unknown time"}</span> : null}</span>
            </button>
            <TaskNamespacePills compact namespaces={task.namespaces} options={namespaceOptions} readOnly={readOnly || Boolean(task.deleted_at) || task.status === "trash" || !onNamespacesChange} onChange={(next, mutationId) => onNamespacesChange ? onNamespacesChange(task, next, mutationId) : Promise.resolve({ intent_id: mutationId ?? "read-only", status: "unavailable", message: "Task editing is unavailable." })} />
            </div>
            {triage ? (
              <div className="wb-task-list__triage" aria-label={`Triage ${task.title}`}>
                <Button size="small" disabled={readOnly} onClick={() => onAction(task, "mit")}>
                  Most Important this week
                </Button>
                <Button size="small" disabled={readOnly} onClick={() => onAction(task, "focus")}>
                  Working on now
                </Button>
                <Button help={{ ...TASK_HELP.snooze, summary: "Snooze this task until tomorrow." }} size="small" disabled={readOnly} onClick={() => onAction(task, "snooze")}>Snooze</Button>
                <Button size="small" disabled={readOnly} onClick={() => onAction(task, "archive")}>Archive</Button>
                <Button
                  size="small"
                  variant="ghost"
                  aria-label={`Skip ${task.title} this pass`}
                  onClick={() => onSkip(task)}
                >
                  Skip this pass
                </Button>
              </div>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}
