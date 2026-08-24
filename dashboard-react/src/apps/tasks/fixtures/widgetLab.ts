import { TASKS_INSTANCE_IDS } from "../bindings";
import type {
  TaskAccess,
  TaskOptions,
  TaskQuickAddInput,
  TaskWorkspaceInput,
} from "../contracts";

const OPTIONS: TaskOptions = {
  projects: [{ value: "work-buddy", label: "Work Buddy" }],
  namespaces: [{ value: "systems/tasks", label: "Systems / Tasks" }],
  contracts: [],
  contexts: [],
};

const access = (readOnly: boolean): TaskAccess =>
  readOnly
    ? { mode: "read_only", reason: "Widget Lab read-only fixture." }
    : { mode: "read_write" };

export interface TasksWidgetLabInputs {
  readonly quickAdd: TaskQuickAddInput;
  readonly workspace: TaskWorkspaceInput;
}

/** Deterministic, network-free inputs for the registered Tasks renderers. */
export const tasksWidgetLabInputs = (readOnly = false): TasksWidgetLabInputs => ({
  quickAdd: {
    instanceId: TASKS_INSTANCE_IDS.quickAdd,
    revision: 17,
    access: access(readOnly),
    options: OPTIONS,
  },
  workspace: {
    instanceId: TASKS_INSTANCE_IDS.workspace,
    revision: 17,
    access: access(readOnly),
    query: {
      lens: "inbox",
      q: "",
      project: "",
      namespace: "",
      urgency: "",
      due: "",
      state: "",
      note: "",
      task: null,
    },
    facets: {
      counts: {
        focused: 0,
        inbox: 0,
        active: 0,
        snoozed: 0,
        completed: 0,
        trash: 0,
        triage: 0,
      },
      projects: {},
      namespaces: {},
      urgencies: {},
    },
    tasks: [],
    selectedTask: null,
    options: OPTIONS,
  },
});
