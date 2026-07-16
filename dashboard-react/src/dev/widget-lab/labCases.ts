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
  throw new Error(
    `Widget Lab needs a deterministic Journal binding for ${widgetTypeId}`,
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
  return dashboardRegistry.listWidgets();
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
