import { useMemo } from "react";

import { useSettingsValues } from "../../../settings/useSettingsValues";
import {
  resolveCoworkShortcutBindings,
  type CoworkShortcutBindings,
} from "./bindings";
import {
  COWORK_SETTINGS_PAGE_ID,
  readCoworkShortcutBindingValue,
} from "./settings";

/**
 * Resolve the effective Co-work review shortcut map from the settings registry. The complete
 * map falls back atomically when the value is absent, invalid, or unavailable, so Queue
 * navigation and decision actions always agree on one effective configuration.
 */
export function useCoworkShortcutBindings(
  contextId: string = COWORK_SETTINGS_PAGE_ID,
): CoworkShortcutBindings {
  const { snapshot } = useSettingsValues(contextId);
  return useMemo(
    () => resolveCoworkShortcutBindings(readCoworkShortcutBindingValue(snapshot)),
    [snapshot],
  );
}
