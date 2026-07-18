import type { Editor } from "@tiptap/core";
import { describe, expect, it, vi } from "vitest";

import {
  FORBIDDEN_INSERT_TYPES,
  SLASH_COMMANDS,
  filterSlashCommands,
  groupSlashCommands,
  moveActiveIndex,
} from "./slashCommands";

/** A chain spy that records every command-chain call and returns itself, run resolving true. */
function makeEditorSpy() {
  const calls: Array<[string, unknown[]]> = [];
  const chain: Record<string, (...args: unknown[]) => unknown> = new Proxy(
    {},
    {
      get(_target, prop: string) {
        return (...args: unknown[]) => {
          calls.push([prop, args]);
          return prop === "run" ? true : chain;
        };
      },
    },
  ) as Record<string, (...args: unknown[]) => unknown>;
  const editor = { chain: () => chain } as unknown as Editor;
  return { editor, calls };
}

describe("filterSlashCommands", () => {
  it("returns the whole registry for an empty query", () => {
    expect(filterSlashCommands("")).toHaveLength(SLASH_COMMANDS.length);
    expect(filterSlashCommands("   ")).toHaveLength(SLASH_COMMANDS.length);
  });

  it("matches a title substring case-insensitively", () => {
    const ids = filterSlashCommands("HEAD").map((command) => command.id);
    expect(ids).toEqual(["heading-1", "heading-2", "heading-3"]);
  });

  it("matches an alias that never shows in the title", () => {
    expect(filterSlashCommands("todo").map((c) => c.id)).toEqual(["task-list"]);
    expect(filterSlashCommands("unordered").map((c) => c.id)).toEqual([
      "bullet-list",
    ]);
  });

  it("returns nothing when no title or alias matches", () => {
    expect(filterSlashCommands("zzzznope")).toEqual([]);
  });
});

describe("slash menu insert-type exclusion", () => {
  it("never offers a suggestion or provenance or expression mark type", () => {
    const offered = SLASH_COMMANDS.map((command) => command.nodeName);
    for (const forbidden of FORBIDDEN_INSERT_TYPES) {
      expect(offered).not.toContain(forbidden);
    }
  });

  it("offers only block-level node types", () => {
    const allowedBlocks = new Set([
      "paragraph",
      "heading",
      "bulletList",
      "orderedList",
      "taskList",
      "blockquote",
      "codeBlock",
      "horizontalRule",
      "table",
      "image",
    ]);
    for (const command of SLASH_COMMANDS) {
      expect(allowedBlocks.has(command.nodeName)).toBe(true);
    }
  });

  it("keeps every command id unique", () => {
    const ids = SLASH_COMMANDS.map((command) => command.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe("SlashCommand.run", () => {
  it("deletes the slash range then inserts the block, for a heading", () => {
    const command = SLASH_COMMANDS.find((c) => c.id === "heading-2");
    expect(command).toBeDefined();
    const { editor, calls } = makeEditorSpy();
    command?.run(editor, { from: 4, to: 6 });
    const names = calls.map(([name]) => name);
    expect(names).toContain("deleteRange");
    expect(names).toContain("toggleHeading");
    expect(names[names.length - 1]).toBe("run");
    const deleteCall = calls.find(([name]) => name === "deleteRange");
    expect(deleteCall?.[1][0]).toEqual({ from: 4, to: 6 });
    const headingCall = calls.find(([name]) => name === "toggleHeading");
    expect(headingCall?.[1][0]).toEqual({ level: 2 });
  });

  it("inserts a three-by-three header table for the table command", () => {
    const command = SLASH_COMMANDS.find((c) => c.id === "table");
    const { editor, calls } = makeEditorSpy();
    command?.run(editor, { from: 1, to: 2 });
    const tableCall = calls.find(([name]) => name === "insertTable");
    expect(tableCall?.[1][0]).toEqual({
      rows: 3,
      cols: 3,
      withHeaderRow: true,
    });
  });
});

describe("groupSlashCommands", () => {
  it("groups commands in group order and drops empty groups", () => {
    const sections = groupSlashCommands(filterSlashCommands("head"));
    expect(sections).toHaveLength(1);
    expect(sections[0].group).toBe("basic");
    expect(sections[0].commands.map((c) => c.id)).toEqual([
      "heading-1",
      "heading-2",
      "heading-3",
    ]);
  });
});

describe("moveActiveIndex", () => {
  it("wraps forward and backward and clamps an empty list", () => {
    expect(moveActiveIndex(0, 3, 1)).toBe(1);
    expect(moveActiveIndex(2, 3, 1)).toBe(0);
    expect(moveActiveIndex(0, 3, -1)).toBe(2);
    expect(moveActiveIndex(0, 0, 1)).toBe(0);
  });
});

it("filter feeds a stable menu order", () => {
  const spy = vi.fn(filterSlashCommands);
  expect(spy("").map((c) => c.id)).toEqual(SLASH_COMMANDS.map((c) => c.id));
});
