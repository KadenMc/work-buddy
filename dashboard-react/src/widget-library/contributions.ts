import type {
  AppContribution,
  WidgetModule,
} from "../dashboard/contributions/contracts";
import {
  CAPTURE_APP_CONTRIBUTION,
  QUICK_TEXT_CAPTURE_MODULE,
} from "./capture";
import { NOTES_APP_CONTRIBUTION, RUNNING_NOTES_MODULE } from "./notes";
import { DAY_TIMELINE_MODULE, TIMELINE_APP_CONTRIBUTION } from "./timeline";

/** Register these before any view contribution that selects the library types. */
export const WIDGET_LIBRARY_CONTRIBUTIONS = [
  CAPTURE_APP_CONTRIBUTION,
  TIMELINE_APP_CONTRIBUTION,
  NOTES_APP_CONTRIBUTION,
] as const satisfies readonly AppContribution[];

export const WIDGET_LIBRARY_MODULES_BY_APP = new Map<
  AppContribution["appId"],
  readonly WidgetModule[]
>([
  [CAPTURE_APP_CONTRIBUTION.appId, [QUICK_TEXT_CAPTURE_MODULE]],
  [TIMELINE_APP_CONTRIBUTION.appId, [DAY_TIMELINE_MODULE]],
  [NOTES_APP_CONTRIBUTION.appId, [RUNNING_NOTES_MODULE]],
]);

export const WIDGET_LIBRARY_MODULES = [
  QUICK_TEXT_CAPTURE_MODULE,
  DAY_TIMELINE_MODULE,
  RUNNING_NOTES_MODULE,
] as const satisfies readonly WidgetModule[];
