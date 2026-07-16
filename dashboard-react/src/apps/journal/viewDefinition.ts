import type { ViewDefinition } from "../../dashboard/contributions/contracts";
import {
  JOURNAL_APP_ID,
  JOURNAL_BINDING_KEYS,
  JOURNAL_INSTANCE_IDS,
  JOURNAL_ROLE_IDS,
  JOURNAL_SLOT_IDS,
  JOURNAL_VIEW_DEFINITION_ID,
  JOURNAL_WIDGET_TYPE_IDS,
} from "./bindings";

export const JOURNAL_VIEW_DEFINITION = {
  viewId: JOURNAL_VIEW_DEFINITION_ID,
  definitionVersion: 1,
  ownerAppId: JOURNAL_APP_ID,
  displayName: "Journal",
  route: "journal",
  navigation: {
    label: "Journal",
    order: 10,
    isDefault: true,
  },
  primaryJob:
    "Record the day as it changes and reconcile what happened with what remains intended.",
  grid: { columns: 24 },
  defaultSlots: [
    {
      slotId: JOURNAL_SLOT_IDS.capture,
      defaultInstanceId: JOURNAL_INSTANCE_IDS.capture,
      requiredRole: JOURNAL_ROLE_IDS.capture,
      defaultWidgetTypeId: JOURNAL_WIDGET_TYPE_IDS.capture,
      presence: "required",
      lockedReason:
        "Without a capture surface, Journal cannot record the day as it changes.",
      defaultSettings: {
        defaultTarget: "running_notes",
        defaultModes: { log: "smart", running_notes: "smart" },
      },
      defaultBindings: {
        day: JOURNAL_BINDING_KEYS.day,
        access: JOURNAL_BINDING_KEYS.access,
      },
      defaultLayout: { x: 0, y: 0, w: 8, h: 8 },
      allowedSubstitution: { minimumDefinitionVersion: 1 },
    },
    {
      slotId: JOURNAL_SLOT_IDS.runningNotes,
      defaultInstanceId: JOURNAL_INSTANCE_IDS.runningNotes,
      requiredRole: JOURNAL_ROLE_IDS.runningNotes,
      defaultWidgetTypeId: JOURNAL_WIDGET_TYPE_IDS.runningNotes,
      presence: "default_on",
      defaultSettings: { displayMode: "chronological" },
      defaultBindings: {
        day: JOURNAL_BINDING_KEYS.day,
        access: JOURNAL_BINDING_KEYS.access,
      },
      defaultLayout: { x: 0, y: 8, w: 8, h: 8 },
      allowedSubstitution: { minimumDefinitionVersion: 1 },
    },
    {
      slotId: JOURNAL_SLOT_IDS.timeline,
      defaultInstanceId: JOURNAL_INSTANCE_IDS.timeline,
      requiredRole: JOURNAL_ROLE_IDS.timeline,
      defaultWidgetTypeId: JOURNAL_WIDGET_TYPE_IDS.timeline,
      presence: "required",
      lockedReason:
        "Without the day timeline, Journal cannot reconcile the day's record with its remaining intent.",
      defaultSettings: { renderMode: "timeline", density: "comfortable" },
      defaultBindings: {
        day: JOURNAL_BINDING_KEYS.day,
        quality: JOURNAL_BINDING_KEYS.quality,
      },
      defaultLayout: { x: 8, y: 0, w: 16, h: 16 },
      allowedSubstitution: { minimumDefinitionVersion: 1 },
    },
  ],
  readingOrder: [
    JOURNAL_SLOT_IDS.capture,
    JOURNAL_SLOT_IDS.timeline,
    JOURNAL_SLOT_IDS.runningNotes,
  ],
  mobileOrder: [
    JOURNAL_SLOT_IDS.capture,
    JOURNAL_SLOT_IDS.timeline,
    JOURNAL_SLOT_IDS.runningNotes,
  ],
} as const satisfies ViewDefinition;
