import type { ViewModule } from "../../dashboard/contributions/viewModules";
import { COWORK_VIEW_ID, COWORK_VIEW_MODULE_ID } from "./bindings";

/**
 * Executable binding for the Co-work view, kept beside its serializable contribution.
 * It is a standard-widget-view module so the generic route projection and ViewHost
 * discover it by View ID with no branch on Co-work, and its loaded form builds the
 * standard view runtime (the coarse document-session provider, its label, and the
 * personalization repository). The dashboard registry wires this at the integration join.
 */
export const COWORK_VIEW_MODULE = {
  kind: "standard-widget-view",
  hostContractVersion: 1,
  moduleId: COWORK_VIEW_MODULE_ID,
  viewId: COWORK_VIEW_ID,
  load: () => import("./viewRuntime"),
} satisfies ViewModule;
