import { useMemo } from "react";

import { useSettingsValues } from "../../../settings/useSettingsValues";
import { resolveNavBinding, type CoworkNavBinding } from "./bindings";
import { COWORK_SETTINGS_PAGE_ID, readNavBindingValue } from "./settings";

/**
 * Resolve the effective Co-work review navigation binding from the settings registry. Reads
 * the value snapshot for the Co-work settings context and maps it to a concrete key pair,
 * degrading to the inverted house default when the value is absent, the setting is not yet
 * registered, or the settings service is unavailable. The result is structurally the rail's
 * QueueBindings, so a consumer passes it straight to QueueView.
 */
export function useCoworkNavBinding(
  contextId: string = COWORK_SETTINGS_PAGE_ID,
): CoworkNavBinding {
  const { snapshot } = useSettingsValues(contextId);
  return useMemo(
    () => resolveNavBinding(readNavBindingValue(snapshot)),
    [snapshot],
  );
}
