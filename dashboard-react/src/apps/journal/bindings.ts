import {
  asAppId,
  asViewId,
  asWidgetInstanceId,
  asWidgetRoleId,
  asWidgetSlotId,
  asWidgetTypeId,
  type WidgetIntent,
  type WidgetInstanceId,
  type WidgetRoleId,
  type WidgetSlotId,
  type WidgetTypeId,
} from "../../dashboard/contributions/contracts";
import {
  JOURNAL_VIEW_ID as JOURNAL_DOMAIN_VIEW_ID,
  JOURNAL_WIDGET_INSTANCE_IDS,
  type JournalAccess,
  type JournalDataQuality,
  type JournalDayBinding,
  type JournalDemoSource,
  type JournalIntent,
  type JournalViewModel,
  type JournalWidgetInputs,
} from "./contracts";

export const JOURNAL_APP_ID = asAppId("wb.journal");
export const JOURNAL_VIEW_DEFINITION_ID = asViewId(JOURNAL_DOMAIN_VIEW_ID);

export const JOURNAL_SLOT_IDS = {
  capture: asWidgetSlotId("capture"),
  timeline: asWidgetSlotId("timeline"),
  runningNotes: asWidgetSlotId("running-notes"),
} as const satisfies Readonly<Record<string, WidgetSlotId>>;

export const JOURNAL_INSTANCE_IDS = {
  capture: asWidgetInstanceId(JOURNAL_WIDGET_INSTANCE_IDS.capture),
  timeline: asWidgetInstanceId(JOURNAL_WIDGET_INSTANCE_IDS.timeline),
  runningNotes: asWidgetInstanceId(JOURNAL_WIDGET_INSTANCE_IDS.runningNotes),
} as const satisfies Readonly<Record<string, WidgetInstanceId>>;

/** Roles and implementations are external library contributions, not Journal-owned types. */
export const JOURNAL_ROLE_IDS = {
  capture: asWidgetRoleId("wb.widget-role.capture@1"),
  timeline: asWidgetRoleId("wb.widget-role.day-timeline@1"),
  runningNotes: asWidgetRoleId("wb.widget-role.running-notes@1"),
} as const satisfies Readonly<Record<string, WidgetRoleId>>;

export const JOURNAL_WIDGET_TYPE_IDS = {
  capture: asWidgetTypeId("wb.capture.quick-text"),
  timeline: asWidgetTypeId("wb.timeline.day"),
  runningNotes: asWidgetTypeId("wb.notes.running"),
} as const satisfies Readonly<Record<string, WidgetTypeId>>;

export const JOURNAL_WIDGET_TYPE_BY_INSTANCE: ReadonlyMap<
  WidgetInstanceId,
  WidgetTypeId
> = new Map([
  [JOURNAL_INSTANCE_IDS.capture, JOURNAL_WIDGET_TYPE_IDS.capture],
  [JOURNAL_INSTANCE_IDS.timeline, JOURNAL_WIDGET_TYPE_IDS.timeline],
  [JOURNAL_INSTANCE_IDS.runningNotes, JOURNAL_WIDGET_TYPE_IDS.runningNotes],
]);

export const JOURNAL_BINDING_KEYS = {
  day: "wb.journal.day@1",
  access: "wb.journal.access@1",
  quality: "wb.journal.quality@1",
  source: "wb.journal.source@1",
} as const;

export type JournalBindingKey =
  (typeof JOURNAL_BINDING_KEYS)[keyof typeof JOURNAL_BINDING_KEYS];
export type JournalBindingValue =
  | JournalDayBinding
  | JournalAccess
  | JournalDataQuality
  | JournalDemoSource;
export type JournalViewBindings = Readonly<Record<JournalBindingKey, JournalBindingValue>>;
export type JournalWidgetInput = JournalWidgetInputs[keyof JournalWidgetInputs];

export function createJournalViewBindings(model: JournalViewModel): JournalViewBindings {
  return {
    [JOURNAL_BINDING_KEYS.day]: model.day,
    [JOURNAL_BINDING_KEYS.access]: model.access,
    [JOURNAL_BINDING_KEYS.quality]: model.quality,
    [JOURNAL_BINDING_KEYS.source]: model.source,
  };
}

/**
 * Applies Core's nominal wire identities at the Journal provider boundary. Wave 1
 * fixtures intentionally keep literal IDs so their computed object keys stay precise.
 */
export function toDashboardJournalIntent(intent: JournalIntent): WidgetIntent<unknown> {
  return {
    ...intent,
    view_id: JOURNAL_VIEW_DEFINITION_ID,
    instance_id: asWidgetInstanceId(intent.instance_id),
  };
}

export function isJournalWidgetInstanceId(value: string): value is JournalWidgetInstanceId {
  return Object.values(JOURNAL_WIDGET_INSTANCE_IDS).some((instanceId) => instanceId === value);
}

export type JournalWidgetInstanceId =
  (typeof JOURNAL_WIDGET_INSTANCE_IDS)[keyof typeof JOURNAL_WIDGET_INSTANCE_IDS];
