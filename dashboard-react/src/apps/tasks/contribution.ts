import type {
  AppContribution,
  JsonSchemaReference,
  WidgetDefinition,
  WidgetIntentEffectDeclaration,
  WidgetRoleContract,
} from "../../dashboard/contributions/contracts";
import {
  TASKS_APP_ID,
  TASKS_ROLE_IDS,
  TASKS_WIDGET_MODULE_IDS,
  TASKS_WIDGET_TYPE_IDS,
} from "./bindings";
import { TASK_INTENTS } from "./contracts";
import { TASKS_VIEW_DEFINITION, TASKS_WIDGET_THEME } from "./viewDefinition";

const QUICK_ADD_INPUT: JsonSchemaReference = {
  schemaId: "wb.tasks.quick-add.input",
  version: 1,
};
const WORKSPACE_INPUT: JsonSchemaReference = {
  schemaId: "wb.tasks.workspace.input",
  version: 1,
};

const intentSchemas = Object.values(TASK_INTENTS).map((schemaId) => ({
  schemaId,
  version: 1,
})) as readonly JsonSchemaReference[];

const mutationTypes = new Set<string>([
  TASK_INTENTS.create,
  TASK_INTENTS.batchCreate,
  TASK_INTENTS.update,
  TASK_INTENTS.complete,
  TASK_INTENTS.reopen,
  TASK_INTENTS.focus,
  TASK_INTENTS.snooze,
  TASK_INTENTS.archive,
  TASK_INTENTS.unarchive,
  TASK_INTENTS.delete,
  TASK_INTENTS.restore,
  TASK_INTENTS.replaceTags,
  TASK_INTENTS.createDocument,
  TASK_INTENTS.localFileAction,
  TASK_INTENTS.actionItemCreate,
  TASK_INTENTS.actionItemUpdate,
  TASK_INTENTS.actionItemReorder,
  TASK_INTENTS.actionItemCurrent,
  TASK_INTENTS.actionItemApprove,
  TASK_INTENTS.actionItemDelete,
  TASK_INTENTS.actionItemRestore,
]);
const readTypes = new Set<string>([TASK_INTENTS.batchPreview]);

const effects = (schemas: readonly JsonSchemaReference[]): readonly WidgetIntentEffectDeclaration[] =>
  schemas.map((schema) => ({
    schema,
    effect: mutationTypes.has(schema.schemaId)
      ? "mutation"
      : readTypes.has(schema.schemaId)
        ? "read"
        : "navigation",
    preview: "block",
  }));

const roles: readonly WidgetRoleContract[] = [
  {
    roleId: TASKS_ROLE_IDS.quickAdd,
    ownerAppId: TASKS_APP_ID,
    displayName: "Task quick add",
    description: "Create one or many authoritative tasks quickly.",
    inputSchema: QUICK_ADD_INPUT,
    outputIntentSchemas: intentSchemas.filter(({ schemaId }) =>
      schemaId === TASK_INTENTS.create ||
      schemaId === TASK_INTENTS.batchPreview ||
      schemaId === TASK_INTENTS.batchCreate,
    ),
  },
  {
    roleId: TASKS_ROLE_IDS.workspace,
    ownerAppId: TASKS_APP_ID,
    displayName: "Task workspace",
    description: "Find, edit, and manage authoritative tasks.",
    inputSchema: WORKSPACE_INPUT,
    outputIntentSchemas: intentSchemas.filter(({ schemaId }) =>
      schemaId !== TASK_INTENTS.create &&
      schemaId !== TASK_INTENTS.batchPreview &&
      schemaId !== TASK_INTENTS.batchCreate,
    ),
  },
];

const quickAddSchemas = roles[0]!.outputIntentSchemas ?? [];
const workspaceSchemas = roles[1]!.outputIntentSchemas ?? [];

const widgets: readonly WidgetDefinition[] = [
  {
    typeId: TASKS_WIDGET_TYPE_IDS.quickAdd,
    definitionVersion: 1,
    publisherAppId: TASKS_APP_ID,
    displayName: "Quick Add",
    description: "Capture an Inbox task in one gesture or preview a pasted batch.",
    libraryPath: ["Tasks", "Quick Add"],
    providesRoles: [TASKS_ROLE_IDS.quickAdd],
    settingsSchema: { schemaId: "wb.tasks.quick-add.settings", version: 1 },
    inputSchema: QUICK_ADD_INPUT,
    outputIntentSchemas: quickAddSchemas,
    outputIntentEffects: effects(quickAddSchemas),
    drafts: [
      {
        draftName: "task-create",
        schema: { schemaId: "wb.tasks.create.draft", version: 1 },
        persistence: "device",
        sensitivity: "ordinary",
        retentionDays: 30,
        maxBytes: 131_072,
        clearPolicy: "confirm",
        scope: { kind: "view" },
      },
    ],
    sizeContract: {
      default: { w: 24, h: 6 },
      min: { w: 8, h: 5 },
      max: { w: 24, h: 12 },
      modes: ["compact", "standard", "expanded"],
    },
    multiplicity: "single_per_view",
    rendererModuleId: TASKS_WIDGET_MODULE_IDS.quickAdd,
    theme: TASKS_WIDGET_THEME,
  },
  {
    typeId: TASKS_WIDGET_TYPE_IDS.workspace,
    definitionVersion: 1,
    publisherAppId: TASKS_APP_ID,
    displayName: "Task Workspace",
    description: "Search, triage, edit, and manage the task collection.",
    libraryPath: ["Tasks", "Workspace"],
    providesRoles: [TASKS_ROLE_IDS.workspace],
    settingsSchema: { schemaId: "wb.tasks.workspace.settings", version: 1 },
    inputSchema: WORKSPACE_INPUT,
    outputIntentSchemas: workspaceSchemas,
    outputIntentEffects: effects(workspaceSchemas),
    drafts: [
      {
        draftName: "task-edit",
        schema: { schemaId: "wb.tasks.edit.draft", version: 1 },
        persistence: "device",
        sensitivity: "ordinary",
        retentionDays: 30,
        maxBytes: 131_072,
        clearPolicy: "confirm",
        scope: { kind: "input-field", path: ["selectedTask", "task_id"] },
      },
    ],
    sizeContract: {
      default: { w: 24, h: 20 },
      min: { w: 12, h: 12 },
      max: { w: 24, h: 40 },
      modes: ["compact", "standard", "expanded"],
    },
    multiplicity: "single_per_view",
    rendererModuleId: TASKS_WIDGET_MODULE_IDS.workspace,
    theme: TASKS_WIDGET_THEME,
  },
];

export const TASKS_APP_CONTRIBUTION = {
  schemaVersion: 1,
  appId: TASKS_APP_ID,
  definitionVersion: 1,
  displayName: "Tasks",
  widgetRoles: roles,
  widgetDefinitions: widgets,
  views: [TASKS_VIEW_DEFINITION],
} as const satisfies AppContribution;
