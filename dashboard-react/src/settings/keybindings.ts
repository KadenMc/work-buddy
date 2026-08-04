import type { KeybindingCommandDefinition } from "./contracts";

export type KeybindingMap = Readonly<Record<string, string>>;

export interface KeybindingValidationIssue {
  readonly commandId?: string;
  readonly message: string;
}

const MODIFIER_KEYS = new Set([
  "Alt",
  "AltGraph",
  "Control",
  "Meta",
  "Shift",
]);

const NAMED_KEYS = new Set([
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  "ArrowUp",
  "Backspace",
  "Delete",
  "End",
  "Enter",
  "Home",
  "Insert",
  "PageDown",
  "PageUp",
  "Space",
]);

function normalizedKeyName(value: string): string | null {
  if (value === " ") return "Space";
  if (value.length === 1 && value !== "+" && !/\s/u.test(value)) {
    return value.toLowerCase();
  }
  if (NAMED_KEYS.has(value) || /^F(?:[1-9]|1\d|2[0-4])$/u.test(value)) {
    return value;
  }
  return null;
}

/** Normalize a portable shortcut chord such as `a` or `Mod+Shift+k`. */
export function normalizeShortcutChord(value: string): string | null {
  const parts = value.trim().split("+");
  if (parts.length === 0 || parts.some((part) => part.length === 0)) return null;
  const key = normalizedKeyName(parts[parts.length - 1] ?? "");
  if (key === null) return null;

  const modifiers = new Set<string>();
  for (const raw of parts.slice(0, -1)) {
    const modifier =
      raw === "Control" || raw === "Ctrl" || raw === "Meta" || raw === "Command"
        ? "Mod"
        : raw === "Option"
          ? "Alt"
          : raw;
    if (modifier !== "Mod" && modifier !== "Alt" && modifier !== "Shift") {
      return null;
    }
    if (modifiers.has(modifier)) return null;
    modifiers.add(modifier);
  }

  return ["Mod", "Alt", "Shift"]
    .filter((modifier) => modifiers.has(modifier))
    .concat(key)
    .join("+");
}

/** Turn one DOM keyboard event into the same portable chord representation. */
export function shortcutChordFromEvent(event: KeyboardEvent): string | null {
  if (MODIFIER_KEYS.has(event.key)) return null;
  const key = normalizedKeyName(event.key);
  if (key === null) return null;
  return [
    event.ctrlKey || event.metaKey ? "Mod" : null,
    event.altKey ? "Alt" : null,
    event.shiftKey ? "Shift" : null,
    key,
  ]
    .filter((part): part is string => part !== null)
    .join("+");
}

/** Global shortcuts never steal text entry, composition, or already-handled events. */
export function shouldIgnoreShortcutEvent(event: KeyboardEvent): boolean {
  if (event.defaultPrevented || event.isComposing) return true;
  const target = event.target;
  if (!(target instanceof Element)) return false;
  if (
    target.closest(
      "input, textarea, select, [contenteditable]:not([contenteditable='false'])",
    ) !== null
  ) {
    return true;
  }
  if (event.key !== "Enter" && event.key !== " ") return false;
  return (
    target.closest(
      "button, a[href], summary, [role='button'], [role='link'], [role='menuitem'], [role='option'], [role='tab'], [role='checkbox'], [role='radio'], [role='switch']",
    ) !== null
  );
}

export function shortcutMatchesEvent(
  binding: string,
  event: KeyboardEvent,
): boolean {
  const normalized = normalizeShortcutChord(binding);
  return normalized !== null && normalized === shortcutChordFromEvent(event);
}

export function formatShortcutChord(binding: string): string {
  const normalized = normalizeShortcutChord(binding) ?? binding;
  return normalized
    .split("+")
    .map((part) =>
      part === "Mod"
        ? "Ctrl/⌘"
        : part.length === 1
          ? part.toUpperCase()
          : part,
    )
    .join(" + ");
}

export function shortcutAriaValue(binding: string): string | undefined {
  const normalized = normalizeShortcutChord(binding);
  if (normalized === null) return undefined;
  if (!normalized.split("+").includes("Mod")) return normalized;
  return [
    normalized.replace("Mod", "Control"),
    normalized.replace("Mod", "Meta"),
  ].join(" ");
}

export function keybindingMapsEqual(
  left: KeybindingMap,
  right: KeybindingMap,
  commands: readonly KeybindingCommandDefinition[],
): boolean {
  return commands.every(
    (command) => left[command.commandId] === right[command.commandId],
  );
}

export function validateKeybindingMap(
  value: unknown,
  commands: readonly KeybindingCommandDefinition[],
): readonly KeybindingValidationIssue[] {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return [{ message: "Shortcut bindings must be an object." }];
  }
  const record = value as Record<string, unknown>;
  const expected = new Set(commands.map((command) => command.commandId));
  const issues: KeybindingValidationIssue[] = [];
  const chordOwners = new Map<string, KeybindingCommandDefinition[]>();

  for (const key of Object.keys(record)) {
    if (!expected.has(key)) {
      issues.push({ message: `Unknown shortcut command: ${key}.` });
    }
  }
  for (const command of commands) {
    const raw = record[command.commandId];
    if (typeof raw !== "string") {
      issues.push({
        commandId: command.commandId,
        message: `${command.label} needs a shortcut.`,
      });
      continue;
    }
    const chord = normalizeShortcutChord(raw);
    if (chord === null || chord !== raw) {
      issues.push({
        commandId: command.commandId,
        message: `${command.label} has an invalid shortcut.`,
      });
      continue;
    }
    if (chord === "Escape" || chord === "Tab") {
      issues.push({
        commandId: command.commandId,
        message: `${formatShortcutChord(chord)} is reserved for navigation.`,
      });
      continue;
    }
    chordOwners.set(chord, [...(chordOwners.get(chord) ?? []), command]);
  }
  for (const [chord, owners] of chordOwners) {
    if (owners.length < 2) continue;
    const labels = owners.map((owner) => owner.label).join(" and ");
    const message = `${labels} conflict: ${owners.length === 2 ? "both" : "all"} use ${formatShortcutChord(chord)}. Assign different shortcuts.`;
    for (const owner of owners) {
      issues.push({ commandId: owner.commandId, message });
    }
  }
  return issues;
}

export function coerceKeybindingMap(
  value: unknown,
  fallback: KeybindingMap,
  commands: readonly KeybindingCommandDefinition[],
): KeybindingMap {
  return validateKeybindingMap(value, commands).length === 0
    ? { ...(value as Record<string, string>) }
    : { ...fallback };
}
