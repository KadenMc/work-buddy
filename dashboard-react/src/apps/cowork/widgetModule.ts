import type { WidgetModule } from "../../dashboard/contributions/contracts";
import { COWORK_WORKSPACE_MODULE_ID, COWORK_WORKSPACE_TYPE_ID } from "./bindings";

/**
 * Lazy renderer binding for the composite Co-work workspace card, kept beside its
 * serializable contribution. The dashboard registry wires this at the integration join,
 * and the durable WidgetHost loads the renderer once and keeps it mounted across every
 * grid remount, customize toggle, and interaction recovery.
 */
export const COWORK_WORKSPACE_WIDGET_MODULE: WidgetModule = {
  moduleId: COWORK_WORKSPACE_MODULE_ID,
  widgetTypeId: COWORK_WORKSPACE_TYPE_ID,
  load: () => import("./widget/CoworkWorkspaceWidget"),
};
