/**
 * The Co-work configurable keyboard layer (PRD section 7). The inverted j/k pair is the
 * default, overridable through a settings-registry value. A consumer reads the effective
 * binding with useCoworkNavBinding and passes it to the rail's QueueView, and the Settings UI
 * renders the contribution below.
 */

export {
  DEFAULT_COWORK_NAV_BINDING,
  DEFAULT_NAV_BINDING_PRESET,
  NAV_BINDING_PRESETS,
  isNavBindingPreset,
  resolveNavBinding,
  type CoworkNavBinding,
  type CoworkNavBindingPreset,
} from "./bindings";
export {
  COWORK_NAV_BINDING_SETTING_ID,
  COWORK_SETTINGS_PAGE_ID,
  coworkKeyboardSettingsContribution,
  readNavBindingValue,
} from "./settings";
export { useCoworkNavBinding } from "./useCoworkNavBinding";
