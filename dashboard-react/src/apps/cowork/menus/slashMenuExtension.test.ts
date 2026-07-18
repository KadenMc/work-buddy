import type { Editor } from "@tiptap/core";
import { describe, expect, it, vi } from "vitest";

import {
  SLASH_MENU_PLUGIN_KEY,
  buildSlashSuggestionConfig,
} from "./slashMenuExtension";
import { SLASH_COMMANDS, type SlashCommand } from "./slashCommands";

describe("buildSlashSuggestionConfig", () => {
  it("triggers on the slash character and binds the plugin key", () => {
    const config = buildSlashSuggestionConfig();
    expect(config.char).toBe("/");
    expect(config.pluginKey).toBe(SLASH_MENU_PLUGIN_KEY);
    expect(config.allowSpaces).toBe(false);
  });

  it("filters the block registry by the live query", () => {
    const config = buildSlashSuggestionConfig();
    const editor = {} as Editor;
    const controller = new AbortController();
    const items = config.items?.({
      query: "quote",
      editor,
      signal: controller.signal,
    });
    expect(Array.isArray(items)).toBe(true);
    expect((items as SlashCommand[]).map((c) => c.id)).toEqual(["quote"]);
  });

  it("opens showing the whole registry before any query is typed", () => {
    const config = buildSlashSuggestionConfig();
    const editor = {} as Editor;
    const items = config.items?.({
      query: "",
      editor,
      signal: new AbortController().signal,
    }) as SlashCommand[];
    expect(items).toHaveLength(SLASH_COMMANDS.length);
  });

  it("dispatches the chosen command's run with the editor and range", () => {
    const config = buildSlashSuggestionConfig();
    const editor = {} as Editor;
    const range = { from: 3, to: 5 };
    const run = vi.fn();
    const command: SlashCommand = {
      id: "probe",
      title: "Probe",
      hint: "",
      aliases: [],
      group: "basic",
      nodeName: "paragraph",
      run,
    };
    config.command?.({ editor, range, props: command });
    expect(run).toHaveBeenCalledWith(editor, range);
  });
});
