import type { WidgetModule } from "../../dashboard/contributions/contracts";
import { TASKS_WIDGET_MODULE_IDS, TASKS_WIDGET_TYPE_IDS } from "./bindings";

export const TASKS_WIDGET_MODULES: readonly WidgetModule[] = [
  {
    moduleId: TASKS_WIDGET_MODULE_IDS.quickAdd,
    widgetTypeId: TASKS_WIDGET_TYPE_IDS.quickAdd,
    load: () => import("./composer/TaskComposer"),
  },
  {
    moduleId: TASKS_WIDGET_MODULE_IDS.workspace,
    widgetTypeId: TASKS_WIDGET_TYPE_IDS.workspace,
    load: () => import("./workspace/TaskWorkspace"),
  },
];
