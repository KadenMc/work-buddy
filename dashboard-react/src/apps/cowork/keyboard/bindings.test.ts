import { describe, expect, it } from "vitest";

import {
  COWORK_SHORTCUT_COMMANDS,
  DEFAULT_COWORK_SHORTCUT_BINDINGS,
  resolveCoworkShortcutBindings,
} from "./bindings";

describe("Co-work review shortcut bindings", () => {
  it("ships one complete, collision-free default map", () => {
    expect(DEFAULT_COWORK_SHORTCUT_BINDINGS).toEqual({
      previous: "j",
      next: "k",
      accept: "a",
      amend: "e",
      reject: "x",
      defer: ".",
    });
    expect(Object.keys(DEFAULT_COWORK_SHORTCUT_BINDINGS).sort()).toEqual(
      COWORK_SHORTCUT_COMMANDS.map((command) => command.commandId).sort(),
    );
    expect(new Set(Object.values(DEFAULT_COWORK_SHORTCUT_BINDINGS)).size).toBe(6);
  });

  it("accepts a complete configured map atomically", () => {
    const configured = {
      previous: "ArrowUp",
      next: "ArrowDown",
      accept: "Mod+Enter",
      amend: "m",
      reject: "r",
      defer: "d",
    };
    expect(resolveCoworkShortcutBindings(configured)).toEqual(configured);
  });

  it("remains defensive around v1 preset values during rolling upgrades", () => {
    expect(resolveCoworkShortcutBindings("inverted")).toEqual(
      DEFAULT_COWORK_SHORTCUT_BINDINGS,
    );
    expect(resolveCoworkShortcutBindings("vim")).toEqual({
      ...DEFAULT_COWORK_SHORTCUT_BINDINGS,
      previous: "k",
      next: "j",
    });
  });

  it("falls back as one map when a value is partial, colliding, or unknown", () => {
    expect(resolveCoworkShortcutBindings({ next: "n" })).toEqual(
      DEFAULT_COWORK_SHORTCUT_BINDINGS,
    );
    expect(
      resolveCoworkShortcutBindings({
        ...DEFAULT_COWORK_SHORTCUT_BINDINGS,
        next: "j",
      }),
    ).toEqual(DEFAULT_COWORK_SHORTCUT_BINDINGS);
    expect(resolveCoworkShortcutBindings("emacs")).toEqual(
      DEFAULT_COWORK_SHORTCUT_BINDINGS,
    );
  });
});
