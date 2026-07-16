import {
  asViewModuleId,
} from "../../dashboard/contributions/contracts";
import type { ViewModule } from "../../dashboard/contributions/viewModules";
import { JOURNAL_VIEW_DEFINITION_ID } from "./bindings";

/**
 * Executable page binding kept beside Journal's serializable contribution data.
 * The generic dashboard registry discovers this module through its View ID; the
 * router never needs to import or branch on Journal itself.
 */
export const JOURNAL_VIEW_MODULE = {
  kind: "standard-widget-view",
  hostContractVersion: 1,
  moduleId: asViewModuleId("wb.journal.view.main.module"),
  viewId: JOURNAL_VIEW_DEFINITION_ID,
  load: () => import("./viewRuntime"),
} satisfies ViewModule;
