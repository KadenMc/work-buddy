import type { KeybindingCommandDefinition } from "../../../settings/contracts";
import {
  coerceKeybindingMap,
  validateKeybindingMap,
  type KeybindingMap,
} from "../../../settings/keybindings";

export const COWORK_SHORTCUT_COMMANDS = [
  {
    commandId: "openClaim",
    label: "Open the claim under the caret",
    description: "Open Truth for the claim at the editor cursor.",
  },
  {
    commandId: "previous",
    label: "Previous review item",
    description: "Move up the Queue.",
  },
  {
    commandId: "next",
    label: "Next review item",
    description: "Move down the Queue.",
  },
  {
    commandId: "accept",
    label: "Accept, endorse, or confirm",
    description: "Choose the positive decision for the current item.",
  },
  {
    commandId: "amend",
    label: "Amend",
    description: "Open the replacement editor for the current suggestion.",
  },
  {
    commandId: "reject",
    label: "Reject or dismiss",
    description: "Choose the direct negative decision for the current item.",
  },
  {
    commandId: "defer",
    label: "Defer",
    description: "Leave the current suggestion for later.",
  },
] as const satisfies readonly KeybindingCommandDefinition[];

export type CoworkShortcutCommandId =
  (typeof COWORK_SHORTCUT_COMMANDS)[number]["commandId"];

export type CoworkShortcutBindings = Readonly<
  Record<CoworkShortcutCommandId, string>
>;

export const DEFAULT_COWORK_SHORTCUT_BINDINGS: CoworkShortcutBindings = {
  openClaim: "Alt+Enter",
  previous: "j",
  next: "k",
  accept: "a",
  amend: "e",
  reject: "x",
  defer: ".",
};

const LEGACY_VIM_BINDINGS: CoworkShortcutBindings = {
  ...DEFAULT_COWORK_SHORTCUT_BINDINGS,
  previous: "k",
  next: "j",
};

/** Resolve the v2 map, while remaining defensive around a stale v1 response. */
export function resolveCoworkShortcutBindings(
  value: unknown,
): CoworkShortcutBindings {
  if (value === "vim") return LEGACY_VIM_BINDINGS;
  if (value === "inverted") return DEFAULT_COWORK_SHORTCUT_BINDINGS;
  const reviewCommands = COWORK_SHORTCUT_COMMANDS.filter((command) => command.commandId !== "openClaim");
  if (validateKeybindingMap(value, reviewCommands).length === 0) {
    const existing = value as Record<string, string>;
    // Every existing configured key retains its meaning during this additive
    // upgrade. Seven distinct candidates guarantee room beside six bindings.
    const openClaim = [
      "Alt+Enter", "Alt+Shift+Enter", "Mod+Alt+Enter", "Mod+Alt+Shift+Enter",
      "Mod+Shift+Enter", "Shift+Enter", "Mod+Enter",
    ].find((chord) => !Object.values(existing).includes(chord))!;
    return { ...existing, openClaim } as CoworkShortcutBindings;
  }
  return coerceKeybindingMap(
    value,
    DEFAULT_COWORK_SHORTCUT_BINDINGS,
    COWORK_SHORTCUT_COMMANDS,
  ) as CoworkShortcutBindings;
}

export function coworkShortcutValueSchema(): object {
  const properties = Object.fromEntries(
    COWORK_SHORTCUT_COMMANDS.map((command) => [
      command.commandId,
      { type: "string" },
    ]),
  );
  return {
    type: "object",
    properties,
    required: COWORK_SHORTCUT_COMMANDS.map((command) => command.commandId),
    additionalProperties: false,
  };
}

export type { KeybindingMap };
