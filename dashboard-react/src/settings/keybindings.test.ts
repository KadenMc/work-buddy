import { describe, expect, it } from "vitest";

import type { KeybindingCommandDefinition } from "./contracts";
import {
  formatShortcutChord,
  normalizeShortcutChord,
  shortcutAriaValue,
  shortcutChordFromEvent,
  shouldIgnoreShortcutEvent,
  validateKeybindingMap,
} from "./keybindings";

const COMMANDS = [
  { commandId: "previous", label: "Previous" },
  { commandId: "next", label: "Next" },
] as const satisfies readonly KeybindingCommandDefinition[];

describe("portable keybindings", () => {
  it("normalizes aliases and modifier order into a stable chord", () => {
    expect(normalizeShortcutChord("Shift+Ctrl+K")).toBe("Mod+Shift+k");
    expect(normalizeShortcutChord("Command+Enter")).toBe("Mod+Enter");
    expect(normalizeShortcutChord("Ctrl+Ctrl+k")).toBeNull();
    expect(normalizeShortcutChord("Escape")).toBeNull();
  });

  it("captures, formats, and exposes a portable chord accessibly", () => {
    const event = new KeyboardEvent("keydown", {
      key: "K",
      ctrlKey: true,
      shiftKey: true,
    });
    expect(shortcutChordFromEvent(event)).toBe("Mod+Shift+k");
    expect(formatShortcutChord("Mod+Shift+k")).toBe("Ctrl/⌘ + Shift + K");
    expect(shortcutAriaValue("Mod+Shift+k")).toBe(
      "Control+Shift+k Meta+Shift+k",
    );
  });

  it("validates exact command coverage and reports collisions by command", () => {
    expect(validateKeybindingMap({ previous: "j", next: "k" }, COMMANDS)).toEqual(
      [],
    );
    expect(validateKeybindingMap({ previous: "j", next: "j" }, COMMANDS)).toEqual([
      {
        commandId: "previous",
        message:
          "Previous and Next conflict: both use J. Assign different shortcuts.",
      },
      {
        commandId: "next",
        message:
          "Previous and Next conflict: both use J. Assign different shortcuts.",
      },
    ]);
    expect(validateKeybindingMap({ previous: "j" }, COMMANDS)).toContainEqual({
      commandId: "next",
      message: "Next needs a shortcut.",
    });
  });

  it("recognizes editable and composing events as unsafe for global dispatch", () => {
    const input = document.createElement("input");
    document.body.append(input);
    const editable = new KeyboardEvent("keydown", { key: "j", bubbles: true });
    input.dispatchEvent(editable);
    expect(shouldIgnoreShortcutEvent(editable)).toBe(true);
    const composing = new KeyboardEvent("keydown", {
      key: "j",
      isComposing: true,
    });
    expect(shouldIgnoreShortcutEvent(composing)).toBe(true);
    input.remove();
  });

  it("does not turn Enter or Space into a second activation on controls", () => {
    const button = document.createElement("button");
    document.body.append(button);
    for (const key of ["Enter", " "]) {
      const activation = new KeyboardEvent("keydown", { key, bubbles: true });
      button.dispatchEvent(activation);
      expect(shouldIgnoreShortcutEvent(activation)).toBe(true);
    }
    const letter = new KeyboardEvent("keydown", { key: "a", bubbles: true });
    button.dispatchEvent(letter);
    expect(shouldIgnoreShortcutEvent(letter)).toBe(false);
    button.remove();
  });
});
