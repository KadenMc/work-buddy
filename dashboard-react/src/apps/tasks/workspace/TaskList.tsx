import { FileText, Flag, Moon, Trash } from "@phosphor-icons/react";
import type { RefObject } from "react";

import { Button } from "../../../ui";
import type { TaskSummary } from "../contracts";

export interface TaskListProps {
  readonly tasks: readonly TaskSummary[];
  readonly selectedTaskId: string | null;
  readonly triage: boolean;
  readonly readOnly: boolean;
  readonly focusRefs: RefObject<Map<string, HTMLButtonElement>>;
  onSelect(taskId: string): void;
  onAction(task: TaskSummary, action: "complete" | "reopen" | "focus" | "mit" | "snooze" | "archive"): void;
  onSkip(task: TaskSummary): void;
}

const dueLabel = (task: TaskSummary): string | null => {
  if (task.deadline_date) return `Deadline ${task.deadline_date}`;
  if (task.due_date) return `Due ${task.due_date}`;
  return null;
};

export function TaskList({
  tasks,
  selectedTaskId,
  triage,
  readOnly,
  focusRefs,
  onSelect,
  onAction,
  onSkip,
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
        const completed = task.completed_at !== null;
        return (
          <li key={task.task_id} className={task.task_id === selectedTaskId ? "is-selected" : ""}>
            <Button
              size="small"
              variant="ghost"
              className="wb-task-list__complete"
              aria-label={completed ? `Reopen ${task.title}` : `Complete ${task.title}`}
              disabled={readOnly}
              onClick={() => onAction(task, completed ? "reopen" : "complete")}
            >
              <span aria-hidden="true">{completed ? "↻" : "✓"}</span>
            </Button>
            <button
              ref={(node) => {
                if (node === null) focusRefs.current?.delete(task.task_id);
                else focusRefs.current?.set(task.task_id, node);
              }}
              type="button"
              className="wb-task-list__select"
              aria-current={task.task_id === selectedTaskId ? "true" : undefined}
              onClick={() => onSelect(task.task_id)}
            >
              <span className="wb-task-list__title">{task.title}</span>
              <span className="wb-task-list__meta">
                <span className={`wb-task-badge wb-task-badge--${task.urgency}`}>
                  <Flag aria-hidden="true" /> {task.urgency}
                </span>
                <span>{task.attention_state}</span>
                {task.project ? <span>{task.project}</span> : null}
                {dueLabel(task) ? <span>{dueLabel(task)}</span> : null}
                {task.current_action ? <span>Next: {task.current_action}</span> : null}
                {task.has_document ? <span><FileText aria-hidden="true" /> Knowledge</span> : null}
                {task.snooze_until ? <span><Moon aria-hidden="true" /> {task.snooze_until}</span> : null}
                {task.deleted_at ? <span><Trash aria-hidden="true" /> Trash</span> : null}
              </span>
            </button>
            {triage ? (
              <div className="wb-task-list__triage" aria-label={`Triage ${task.title}`}>
                <Button size="small" disabled={readOnly} onClick={() => onAction(task, "mit")}>
                  Most Important this week
                </Button>
                <Button size="small" disabled={readOnly} onClick={() => onAction(task, "focus")}>
                  Working on now
                </Button>
                <Button size="small" disabled={readOnly} onClick={() => onAction(task, "snooze")}>Snooze</Button>
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
