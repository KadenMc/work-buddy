import { createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { Extension } from "@tiptap/core";
import { PluginKey } from "@tiptap/pm/state";
import {
  Suggestion,
  type SuggestionOptions,
  type SuggestionProps,
} from "@tiptap/suggestion";

import { SlashMenu } from "./SlashMenu";
import {
  filterSlashCommands,
  moveActiveIndex,
  type SlashCommand,
} from "./slashCommands";

/**
 * The slash menu (PRD section 7 writing walkthrough). A `@tiptap/suggestion` plugin detects
 * the `/` trigger and offers the editor block types through a cmdk-rendered popup, mounted
 * and positioned by the plugin's built-in floating-ui mount. The insert menu carries block
 * types only, never the tracked-change suggestion marks or the wb provenance / expression
 * decorations (those are ledger-derived, never authored here). Owned by the writing-flow
 * partition, wired into the editor bundle in editor/extensions.ts alongside the suggestion
 * layer.
 */
export const SLASH_MENU_PLUGIN_KEY = new PluginKey("coworkSlashMenu");

/**
 * A single popup session. Created once by the suggestion plugin (render is called once),
 * with per-activation state reset in onStart. The editor keeps DOM focus, so the arrow / enter
 * walk is driven here and forwarded through the React `activeId` prop rather than cmdk's own
 * focus loop.
 */
function createSlashRenderer() {
  let root: Root | null = null;
  let element: HTMLElement | null = null;
  let unmount: (() => void) | null = null;
  let items: SlashCommand[] = [];
  let activeIndex = 0;
  let current: SuggestionProps<SlashCommand> | null = null;

  const pick = (command: SlashCommand): void => {
    current?.command(command);
  };

  const setActiveById = (id: string): void => {
    const next = items.findIndex((command) => command.id === id);
    if (next === -1 || next === activeIndex) return;
    activeIndex = next;
    paint();
  };

  const paint = (): void => {
    if (root === null) return;
    root.render(
      createElement(SlashMenu, {
        commands: items,
        activeId: items[activeIndex]?.id ?? null,
        onSelect: pick,
        onActiveChange: setActiveById,
      }),
    );
  };

  return {
    onStart(props: SuggestionProps<SlashCommand>) {
      current = props;
      items = props.items;
      activeIndex = 0;
      element = document.createElement("div");
      element.className = "wb-cowork-slash-popup";
      root = createRoot(element);
      paint();
      unmount = props.mount(element);
    },

    onUpdate(props: SuggestionProps<SlashCommand>) {
      current = props;
      items = props.items;
      if (activeIndex >= items.length) activeIndex = 0;
      paint();
    },

    onKeyDown({ event }: { event: KeyboardEvent }): boolean {
      if (event.key === "ArrowUp") {
        activeIndex = moveActiveIndex(activeIndex, items.length, -1);
        paint();
        return true;
      }
      if (event.key === "ArrowDown") {
        activeIndex = moveActiveIndex(activeIndex, items.length, 1);
        paint();
        return true;
      }
      if (event.key === "Enter") {
        const command = items[activeIndex];
        if (command === undefined) return false;
        pick(command);
        return true;
      }
      return false;
    },

    onExit() {
      root?.unmount();
      unmount?.();
      root = null;
      element = null;
      unmount = null;
      current = null;
      items = [];
      activeIndex = 0;
    },
  };
}

/**
 * The suggestion configuration, extracted so the trigger character, the item filter, and the
 * command dispatch are unit-testable without a live editor.
 */
export function buildSlashSuggestionConfig(): Omit<
  SuggestionOptions<SlashCommand>,
  "editor"
> {
  return {
    char: "/",
    pluginKey: SLASH_MENU_PLUGIN_KEY,
    allowSpaces: false,
    startOfLine: false,
    items: ({ query }) => filterSlashCommands(query),
    command: ({ editor, range, props }) => {
      props.run(editor, range);
    },
    render: createSlashRenderer,
  };
}

export interface CoworkSlashMenuOptions {
  readonly suggestion: Omit<SuggestionOptions<SlashCommand>, "editor">;
}

/** The editor extension. Added to the editor bundle only, never the DOM-free MarkdownManager. */
export const CoworkSlashMenu = Extension.create<CoworkSlashMenuOptions>({
  name: "coworkSlashMenu",

  addOptions() {
    return { suggestion: buildSlashSuggestionConfig() };
  },

  addProseMirrorPlugins() {
    return [
      Suggestion<SlashCommand>({
        editor: this.editor,
        ...this.options.suggestion,
      }),
    ];
  },
});

export default CoworkSlashMenu;
