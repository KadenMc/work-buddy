import {
  asAppId, asViewId, asViewModuleId, asWidgetInstanceId, asWidgetModuleId,
  asWidgetRoleId, asWidgetSlotId, asWidgetTypeId,
  type AppContribution, type WidgetDefinition, type WidgetModule,
} from "../../dashboard/contributions/contracts";
import type { ViewModule } from "../../dashboard/contributions/viewModules";
import { assistedDraftDeclaration } from "../../dashboard/assistance/schema";
import { JOB_INTENTS } from "./contracts";

export const JOBS_APP_ID = asAppId("wb.jobs");
export const JOBS_VIEW_ID = asViewId("wb.jobs.authoring");
export const JOBS_INSTANCE_ID = asWidgetInstanceId("wb-jobs:authoring");
export const JOBS_WIDGET_ID = asWidgetTypeId("wb.jobs.authoring-card");
const role = asWidgetRoleId("wb.widget-role.job-authoring@1");
const slot = asWidgetSlotId("job-authoring");
const renderer = asWidgetModuleId("wb.jobs.authoring-card.renderer");
const inputSchema = { schemaId: "wb.jobs.authoring.input", version: 1 } as const;
const outputIntentSchemas = Object.values(JOB_INTENTS).map((schemaId) => ({ schemaId, version: 1 }));

export const JOB_AUTHORING_WIDGET: WidgetDefinition = {
  typeId: JOBS_WIDGET_ID, definitionVersion: 1, publisherAppId: JOBS_APP_ID,
  displayName: "Create a scheduled job", description: "Review real job fields while an optional assistant helps fill them.",
  libraryPath: ["Jobs", "Authoring"], providesRoles: [role], settingsSchema: { schemaId: "wb.jobs.authoring.settings", version: 1 },
  inputSchema, outputIntentSchemas,
  outputIntentEffects: outputIntentSchemas.map((schema) => ({ schema, effect: schema.schemaId === JOB_INTENTS.create ? "mutation" : "read", preview: "block" })),
  drafts: [{ draftName: "job-create", schema: { schemaId: "wb.jobs.create.draft", version: 1 }, persistence: "device", sensitivity: "ordinary", retentionDays: 30, maxBytes: 131_072, clearPolicy: "confirm", scope: { kind: "view" } }],
  assistableDrafts: [assistedDraftDeclaration("job-create")],
  sizeContract: { default: { w: 24, h: 19 }, min: { w: 12, h: 12 }, max: { w: 24, h: 35 }, modes: ["compact", "standard", "expanded"] },
  multiplicity: "single_per_view", rendererModuleId: renderer,
  theme: { contractVersion: 1, conformance: "standard", supports: ["light", "dark", "forced-colors", "reduced-motion"], styling: "semantic-tokens" },
};
export const JOBS_APP_CONTRIBUTION: AppContribution = {
  schemaVersion: 1, appId: JOBS_APP_ID, definitionVersion: 1, displayName: "Jobs",
  widgetRoles: [{ roleId: role, ownerAppId: JOBS_APP_ID, displayName: "Job authoring", description: "Human-reviewed scheduled job creation.", inputSchema, outputIntentSchemas }],
  widgetDefinitions: [JOB_AUTHORING_WIDGET],
  views: [{
    viewId: JOBS_VIEW_ID, definitionVersion: 1, ownerAppId: JOBS_APP_ID, displayName: "Jobs", route: "jobs", navigation: { label: "Jobs", order: 40 },
    primaryJob: "Create a scheduled job with visible, user-controlled fields.", grid: { columns: 24 },
    defaultSlots: [{ slotId: slot, defaultInstanceId: JOBS_INSTANCE_ID, requiredRole: role, defaultWidgetTypeId: JOBS_WIDGET_ID, presence: "required", lockedReason: "Jobs keeps its authoring form available.", defaultSettings: {}, defaultLayout: { x: 0, y: 0, w: 24, h: 19 }, allowedSubstitution: { minimumDefinitionVersion: 1 }, help: { summary: "Create a job manually or ask the assistant to fill these fields.", details: "The assistant cannot submit. Create job is the only action that schedules the work." } }],
    readingOrder: [slot], mobileOrder: [slot],
  }],
};
export const JOBS_VIEW_MODULE: ViewModule = { kind: "standard-widget-view", hostContractVersion: 1, moduleId: asViewModuleId("wb.jobs.authoring.module"), viewId: JOBS_VIEW_ID, load: () => import("./viewRuntime") };
export const JOBS_WIDGET_MODULE: WidgetModule = { moduleId: renderer, widgetTypeId: JOBS_WIDGET_ID, load: () => import("./JobComposer") };
