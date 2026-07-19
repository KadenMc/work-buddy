/**
 * The Co-work review keyboard binding (PRD section 7 review walkthrough: "Keyboard walks
 * item to item (j/k, Kaden's inverted binding, as a configurable personal binding"). The
 * binding is a pair of single-key names, structurally the QueueView `QueueBindings` shape,
 * resolved from a settings-registry value with the inverted default when nothing is set.
 */

/** A navigation key pair. Structurally assignable to the rail's QueueBindings. */
export interface CoworkNavBinding {
  /** The key that moves to the previous item (up the list). */
  readonly prev: string;
  /** The key that moves to the next item (down the list). */
  readonly next: string;
}

/** The selectable preset ids exposed by the setting. */
export type CoworkNavBindingPreset = "inverted" | "vim";

/**
 * The two presets. `inverted` is the house binding (j moves up / previous, k moves down /
 * next), `vim` is the conventional vim pair (j down / next, k up / previous). The presets are
 * the only setting values, so a keybinding never carries a raw keycode across the wire.
 */
export const NAV_BINDING_PRESETS: Record<
  CoworkNavBindingPreset,
  CoworkNavBinding
> = {
  inverted: { prev: "j", next: "k" },
  vim: { prev: "k", next: "j" },
};

/** The default preset id and its binding (the inverted house pair). */
export const DEFAULT_NAV_BINDING_PRESET: CoworkNavBindingPreset = "inverted";
export const DEFAULT_COWORK_NAV_BINDING: CoworkNavBinding =
  NAV_BINDING_PRESETS[DEFAULT_NAV_BINDING_PRESET];

/** Whether a value is a known preset id. */
export function isNavBindingPreset(
  value: unknown,
): value is CoworkNavBindingPreset {
  return value === "inverted" || value === "vim";
}

/**
 * Resolve a stored setting value to a concrete binding. An unknown or absent value degrades
 * to the inverted default, so the review keyboard always works even before the setting is
 * registered server-side.
 */
export function resolveNavBinding(value: unknown): CoworkNavBinding {
  return isNavBindingPreset(value)
    ? NAV_BINDING_PRESETS[value]
    : DEFAULT_COWORK_NAV_BINDING;
}
