import {
  asAppId,
  asViewId,
  asViewModuleId,
  asWidgetInstanceId,
  asWidgetModuleId,
  asWidgetRoleId,
  asWidgetSlotId,
  asWidgetTypeId,
} from "../../dashboard/contributions/contracts";

export const TASKS_APP_ID = asAppId("wb.tasks");
export const TASKS_VIEW_ID = asViewId("wb.tasks.workspace");
export const TASKS_ROUTE = "tasks";
export const TASKS_VIEW_MODULE_ID = asViewModuleId("wb.tasks.workspace.module");

export const TASKS_SLOT_IDS = {
  quickAdd: asWidgetSlotId("quick-add"),
  workspace: asWidgetSlotId("workspace"),
} as const;
export const TASKS_INSTANCE_IDS = {
  quickAdd: asWidgetInstanceId("wb-tasks:quick-add"),
  workspace: asWidgetInstanceId("wb-tasks:workspace"),
} as const;

export const TASKS_ROLE_IDS = {
  quickAdd: asWidgetRoleId("wb.widget-role.task-quick-add@1"),
  workspace: asWidgetRoleId("wb.widget-role.task-workspace@1"),
} as const;

export const TASKS_WIDGET_TYPE_IDS = {
  quickAdd: asWidgetTypeId("wb.tasks.quick-add-card"),
  workspace: asWidgetTypeId("wb.tasks.workspace-card"),
} as const;

export const TASKS_WIDGET_MODULE_IDS = {
  quickAdd: asWidgetModuleId("wb.tasks.quick-add-card.renderer"),
  workspace: asWidgetModuleId("wb.tasks.workspace-card.renderer"),
} as const;
