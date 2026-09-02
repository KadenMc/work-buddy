import { TextAreaField } from "../../../ui";
import type { TaskOptions, TaskUrgency } from "../contracts";
import type { TaskCreateDraft } from "./taskDraft";

/** The same visible fields serve direct creation and Threads-backed proposal review. */
export function TaskDraftFields({ value, options, disabled, idPrefix, errors = {}, update, fieldProps }: {
  readonly value: TaskCreateDraft;
  readonly options: TaskOptions;
  readonly disabled: boolean;
  readonly idPrefix: string;
  readonly errors?: Readonly<Record<string, string>>;
  readonly update: <Key extends keyof TaskCreateDraft>(key: Key, next: TaskCreateDraft[Key]) => void;
  readonly fieldProps?: (path: readonly string[]) => Readonly<Record<string, unknown>>;
}) {
  const error = (key: string, ...aliases: string[]) => Object.entries(errors).find(([name]) =>
    [key, ...aliases].some((candidate) => name === candidate || name.startsWith(`${candidate}.`)))?.[1];
  const props = (key: string, ...aliases: string[]) => ({
    ...fieldProps?.([key]), disabled,
    "aria-invalid": error(key, ...aliases) ? "true" as const : undefined,
    "aria-describedby": error(key, ...aliases) ? `${idPrefix}-${key}-error` : undefined,
  });
  const hint = (key: string, ...aliases: string[]) => error(key, ...aliases)
    ? <small id={`${idPrefix}-${key}-error`} className="wb-task-field-error">{error(key, ...aliases)}</small> : null;
  return <div className="wb-task-composer__details">
    <label className="wb-task-field"><span>State</span><select {...props("attention_state", "state")} value={value.attention_state} onChange={(event) => update("attention_state", event.target.value)}><option value="inbox">Inbox</option><option value="mit">Most Important</option><option value="active">Active</option><option value="focused">Focused</option><option value="waiting">Waiting</option></select>{hint("attention_state", "state")}</label>
    <label className="wb-task-field"><span>Urgency</span><select {...props("urgency")} value={value.urgency} onChange={(event) => update("urgency", event.target.value as TaskUrgency)}><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option></select>{hint("urgency")}</label>
    <label className="wb-task-field"><span>Due date</span><input {...props("due_date")} type="date" value={value.due_date} onChange={(event) => update("due_date", event.target.value)} />{hint("due_date")}</label>
    <label className="wb-task-field"><span>Hard deadline</span><input {...props("deadline_date")} type="date" value={value.deadline_date} onChange={(event) => update("deadline_date", event.target.value)} />{hint("deadline_date")}</label>
    <label className="wb-task-field"><span>Project</span><input {...props("project")} list={`${idPrefix}-projects`} value={value.project} onChange={(event) => update("project", event.target.value)} />{hint("project")}</label>
    <label className="wb-task-field"><span>Namespaces</span><input {...props("namespaces")} value={value.namespaces} placeholder="personal, errands" onChange={(event) => update("namespaces", event.target.value)} />{hint("namespaces")}</label>
    <datalist id={`${idPrefix}-projects`}>{options.projects.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</datalist>
    {([
      ["summary", "Summary", "summary_text"], ["desired_outcome", "Desired outcome", "outcome_text"],
      ["next_action", "Next action", "next_action_text"], ["definition_of_done", "Definition of done", "definition_of_done"],
    ] as const).map(([key, label, alias]) => <TextAreaField key={key} {...fieldProps?.([key])} disabled={disabled} label={label} value={value[key]} rows={2} aria-invalid={error(key, alias) ? "true" : undefined} description={error(key, alias)} onChange={(next) => update(key, next)} />)}
    <label className="wb-task-field wb-task-field--wide"><span>Dependencies</span><input {...props("dependencies")} value={value.dependencies} placeholder="Comma-separated" onChange={(event) => update("dependencies", event.target.value)} />{hint("dependencies")}</label>
    <fieldset className="wb-task-field wb-task-field--wide wb-task-note-options">
      <legend>Task note</legend>
      <label>
        <input
          {...props("create_note", "requested_note_role")}
          type="checkbox"
          checked={value.create_note}
          onChange={(event) => {
            update("create_note", event.target.checked);
            if (!event.target.checked) update("enable_truth_tools", false);
          }}
        />
        Create an editable Co-work note
      </label>
      <small>
        The note keeps authorship and human-review provenance for user and AI edits.
      </small>
      {value.create_note ? (
        <>
          <TextAreaField
            {...fieldProps?.(["initial_note"])}
            disabled={disabled}
            label="Initial note"
            value={value.initial_note}
            rows={4}
            aria-invalid={error("initial_note", "note_markdown") ? "true" : undefined}
            description={error("initial_note", "note_markdown")}
            onChange={(next) => update("initial_note", next)}
          />
          <label>
            <input
              {...props("enable_truth_tools", "requested_truth_policy_resolution")}
              type="checkbox"
              checked={value.enable_truth_tools}
              onChange={(event) => update("enable_truth_tools", event.target.checked)}
            />
            Enable Truth tools for this note
          </label>
          <small>
            Truth adds claim and evidence tools. It does not analyze the note automatically.
          </small>
        </>
      ) : null}
      {hint("create_note", "requested_note_role")}
      {hint("enable_truth_tools", "requested_truth_policy_resolution")}
    </fieldset>
  </div>;
}
