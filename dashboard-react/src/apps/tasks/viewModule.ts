import type { ViewModule } from "../../dashboard/contributions/viewModules";
import { TASKS_VIEW_ID, TASKS_VIEW_MODULE_ID } from "./bindings";

export const TASKS_VIEW_MODULE = {
  kind: "standard-widget-view",
  hostContractVersion: 1,
  moduleId: TASKS_VIEW_MODULE_ID,
  viewId: TASKS_VIEW_ID,
  load: () => import("./viewRuntime"),
} satisfies ViewModule;
