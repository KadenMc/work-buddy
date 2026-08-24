import {
  asSettingsPageId,
  type ViewDefinition,
} from "../../dashboard/contributions/contracts";
import type { WidgetThemeDeclaration } from "../../dashboard/contributions/themeContract";
import {
  TASKS_APP_ID,
  TASKS_INSTANCE_IDS,
  TASKS_ROLE_IDS,
  TASKS_ROUTE,
  TASKS_SLOT_IDS,
  TASKS_VIEW_ID,
  TASKS_WIDGET_TYPE_IDS,
} from "./bindings";

export const TASKS_WIDGET_THEME = {
  contractVersion: 1,
  conformance: "standard",
  supports: ["light", "dark", "forced-colors", "reduced-motion"],
  styling: "semantic-tokens",
} as const satisfies WidgetThemeDeclaration;

export const TASKS_VIEW_DEFINITION = {
  viewId: TASKS_VIEW_ID,
  definitionVersion: 1,
  ownerAppId: TASKS_APP_ID,
  displayName: "Tasks",
  route: TASKS_ROUTE,
  navigation: {
    label: "Tasks",
    order: 20,
  },
  primaryJob: "Capture, clarify, and complete work without leaving Tasks.",
  settings: {
    pageId: asSettingsPageId("wb.settings.app.tasks"),
    label: "Task settings",
  },
  grid: { columns: 24 },
  defaultSlots: [
    {
      slotId: TASKS_SLOT_IDS.quickAdd,
      defaultInstanceId: TASKS_INSTANCE_IDS.quickAdd,
      requiredRole: TASKS_ROLE_IDS.quickAdd,
      defaultWidgetTypeId: TASKS_WIDGET_TYPE_IDS.quickAdd,
      presence: "required",
      help: {
        summary: "Capture a task in one gesture.",
        details:
          "Enter creates an Inbox task with medium urgency. Expand details or paste multiple lines when you need more structure.",
      },
      lockedReason:
        "Tasks keeps Quick Add available so creating work stays one gesture away.",
      defaultSettings: {},
      defaultLayout: { x: 0, y: 0, w: 24, h: 6 },
      allowedSubstitution: { minimumDefinitionVersion: 1 },
    },
    {
      slotId: TASKS_SLOT_IDS.workspace,
      defaultInstanceId: TASKS_INSTANCE_IDS.workspace,
      requiredRole: TASKS_ROLE_IDS.workspace,
      defaultWidgetTypeId: TASKS_WIDGET_TYPE_IDS.workspace,
      presence: "required",
      help: {
        summary: "Find, triage, and manage tasks.",
        details:
          "Lenses and filters narrow the task collection; the detail pane edits task fields and opens the bound Co-work knowledge document.",
      },
      lockedReason:
        "Tasks needs the workspace to show and manage your task collection.",
      defaultSettings: {},
      defaultLayout: { x: 0, y: 6, w: 24, h: 20 },
      allowedSubstitution: { minimumDefinitionVersion: 1 },
    },
  ],
  readingOrder: [TASKS_SLOT_IDS.quickAdd, TASKS_SLOT_IDS.workspace],
  mobileOrder: [TASKS_SLOT_IDS.quickAdd, TASKS_SLOT_IDS.workspace],
} as const satisfies ViewDefinition;
