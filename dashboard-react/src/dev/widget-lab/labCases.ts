import { dashboardRegistry } from "../../app/dashboardRegistry";
import {
  JOURNAL_INSTANCE_IDS,
  JOURNAL_WIDGET_TYPE_IDS,
} from "../../apps/journal/bindings";
import { JULY11_INITIAL_MODEL } from "../../apps/journal/fixtures/july11";
import {
  JOURNAL_EMPTY_FIXTURE,
  JOURNAL_OFFLINE_FIXTURE,
  JOURNAL_READ_ONLY_FIXTURE,
  JOURNAL_STALE_FIXTURE,
} from "../../apps/journal/fixtures/states";
import { TASKS_WIDGET_TYPE_IDS } from "../../apps/tasks/bindings";
import { tasksWidgetLabInputs } from "../../apps/tasks/fixtures/widgetLab";
import { JOBS_WIDGET_ID } from "../../apps/jobs/contribution";
import type { JobAuthoringInput } from "../../apps/jobs/contracts";
import {
  asViewId,
  asWidgetInstanceId,
  type WidgetInstanceId,
  type WidgetSizeMode,
  type WidgetTypeId,
} from "../../dashboard/contributions/contracts";
import type { RegisteredWidget } from "../../dashboard/contributions/registry";
import type { WidgetHostStatus } from "../../dashboard/widgets/WidgetStates";

export const WIDGET_LAB_VIEW_ID = asViewId("wb.dev.widget-lab");

export const WIDGET_LAB_SIZE_MODES = [
  "compact",
  "standard",
  "expanded",
] as const satisfies readonly WidgetSizeMode[];

export const WIDGET_LAB_HOST_STATES = [
  "ready",
  "loading",
  "empty",
  "stale",
  "offline",
  "unavailable",
  "permission-denied",
  "error",
  "read-only",
] as const satisfies readonly WidgetHostStatus[];

export const WIDGET_LAB_DIMENSIONS: Readonly<
  Record<WidgetSizeMode, { readonly width: number; readonly height: number }>
> = {
  compact: { width: 320, height: 360 },
  standard: { width: 520, height: 520 },
  expanded: { width: 760, height: 680 },
};

const modelForState = (status: WidgetHostStatus) => {
  if (status === "empty") return JOURNAL_EMPTY_FIXTURE.model;
  if (status === "stale") return JOURNAL_STALE_FIXTURE.model;
  if (status === "offline") return JOURNAL_OFFLINE_FIXTURE.model;
  if (status === "read-only") return JOURNAL_READ_ONLY_FIXTURE.model;
  return JULY11_INITIAL_MODEL;
};

const inputForType = (
  widgetTypeId: WidgetTypeId,
  status: WidgetHostStatus,
): unknown => {
  const model = modelForState(status);
  const inputs = model.widgetInputs as unknown as Readonly<
    Record<string, unknown>
  >;
  if (widgetTypeId === JOURNAL_WIDGET_TYPE_IDS.capture) {
    return inputs[JOURNAL_INSTANCE_IDS.capture];
  }
  if (widgetTypeId === JOURNAL_WIDGET_TYPE_IDS.timeline) {
    return inputs[JOURNAL_INSTANCE_IDS.timeline];
  }
  if (widgetTypeId === JOURNAL_WIDGET_TYPE_IDS.runningNotes) {
    return inputs[JOURNAL_INSTANCE_IDS.runningNotes];
  }
  const taskInputs = tasksWidgetLabInputs(status === "read-only");
  if (widgetTypeId === TASKS_WIDGET_TYPE_IDS.quickAdd) {
    return taskInputs.quickAdd;
  }
  if (widgetTypeId === TASKS_WIDGET_TYPE_IDS.workspace) {
    return taskInputs.workspace;
  }
  if (widgetTypeId === JOBS_WIDGET_ID) {
    return {
      access: status === "read-only"
        ? { mode: "read_only", reason: "Widget Lab read-only fixture." }
        : { mode: "read_write" },
      timeZone: "America/New_York",
      capabilities: [{
        name: "journal_state",
        description: "Read the current Journal state.",
        parameters: {},
      }],
      workflows: [{
        name: "morning-routine",
        description: "Prepare a daily work plan.",
        parameters: {},
      }],
      openAssistance: false,
    } satisfies JobAuthoringInput;
  }
  throw new Error(
    `Widget Lab needs a deterministic binding for ${widgetTypeId}`,
  );
};

export interface WidgetLabCase {
  readonly caseId: string;
  readonly widget: RegisteredWidget;
  readonly instanceId: WidgetInstanceId;
  readonly sizeMode: WidgetSizeMode;
  readonly status: WidgetHostStatus;
  readonly input: unknown;
}

function makeCase(
  widget: RegisteredWidget,
  sizeMode: WidgetSizeMode,
  status: WidgetHostStatus,
  ordinal: number,
  group: "mode" | "state" | "trace",
): WidgetLabCase {
  const caseId = `${group}-${ordinal}-${widget.definition.typeId}-${sizeMode}-${status}`;
  const instanceId = asWidgetInstanceId(`wb.dev.widget-lab.${group}.${ordinal}`);
  const sourceInput = inputForType(widget.definition.typeId, status);
  const input =
    typeof sourceInput === "object" && sourceInput !== null
      ? { ...sourceInput, instanceId }
      : sourceInput;
  return { caseId, widget, instanceId, sizeMode, status, input };
}

export function listReusableLabWidgets(): readonly RegisteredWidget[] {
  // Durable widgets (for example the Co-work workspace card) are one app-owned keep-alive
  // instance with their own live state, not reusable snapshot-hydrated widgets. They carry
  // no deterministic Journal binding, so `inputForType` would throw for them. The lab
  // renders the durable Co-work states in its own section (coworkLabCases) instead.
  return dashboardRegistry
    .listWidgets()
    .filter((widget) => widget.definition.durable !== true);
}

export function buildModeCases(): readonly WidgetLabCase[] {
  let ordinal = 0;
  return listReusableLabWidgets().flatMap((widget) =>
    WIDGET_LAB_SIZE_MODES.map((sizeMode) =>
      makeCase(widget, sizeMode, "ready", ordinal++, "mode"),
    ),
  );
}

export function buildStateCases(): readonly WidgetLabCase[] {
  let ordinal = 0;
  return listReusableLabWidgets().flatMap((widget) =>
    WIDGET_LAB_HOST_STATES.map((status) =>
      makeCase(widget, "standard", status, ordinal++, "state"),
    ),
  );
}

export function buildSyntheticTraceCases(count: number): readonly WidgetLabCase[] {
  const widgets = listReusableLabWidgets();
  if (widgets.length === 0) return [];
  return Array.from({ length: count }, (_, ordinal) =>
    makeCase(
      widgets[ordinal % widgets.length]!,
      WIDGET_LAB_SIZE_MODES[ordinal % WIDGET_LAB_SIZE_MODES.length]!,
      "ready",
      ordinal,
      "trace",
    ),
  );
}
