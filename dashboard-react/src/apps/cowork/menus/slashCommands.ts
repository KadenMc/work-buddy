import type { Editor } from "@tiptap/core";

/**
 * The slash menu command registry (PRD section 7 writing walkthrough, "Slash menu
 * (cmdk) inserts structure"). The menu offers editor BLOCK types only. The tracked-change
 * suggestion marks (insertion / deletion / modification) and the provenance and expression
 * marks are review-layer decorations, never insertable structure, so they are absent from
 * this registry by construction and the exclusion is asserted in the tests.
 *
 * Each command deletes the slash query range and inserts its block through the ordinary
 * editor command chain, so a slash insert is a direct human edit (human-origin), never a
 * proposal.
 */

/** The range of the slash trigger, deleted before the block is inserted. */
export interface SlashRange {
  readonly from: number;
  readonly to: number;
}

/** The visual grouping a command sits under in the menu. */
export type SlashCommandGroup = "basic" | "lists" | "blocks" | "media";

export interface SlashCommand {
  /** Stable identity, also the cmdk item value. */
  readonly id: string;
  /** The label shown in the menu. */
  readonly title: string;
  /** A one-line description shown under the title. */
  readonly hint: string;
  /** Extra match terms beyond the title (never shown). */
  readonly aliases: readonly string[];
  readonly group: SlashCommandGroup;
  /**
   * The primary block-level node this command inserts. Recorded so the tests can prove the
   * menu never offers a mark type, and never anything outside the editor's block schema.
   */
  readonly nodeName: string;
  /** Insert the block, deleting the slash query range first. */
  run(editor: Editor, range: SlashRange): void;
}

/** Human-readable labels for the group headings, in menu order. */
export const SLASH_GROUP_LABEL: Record<SlashCommandGroup, string> = {
  basic: "Basic",
  lists: "Lists",
  blocks: "Blocks",
  media: "Media",
};

export const SLASH_GROUP_ORDER: readonly SlashCommandGroup[] = [
  "basic",
  "lists",
  "blocks",
  "media",
];

/**
 * The mark types that must NEVER appear as insert commands. The suggestion marks are the
 * tracked-change layer and the two wb marks are review decorations, all ledger-derived and
 * never authored from the insert menu (C1 surface section 6, gate condition on the insert
 * menu). Referenced by the exclusion test.
 */
export const FORBIDDEN_INSERT_TYPES: readonly string[] = [
  "insertion",
  "deletion",
  "modification",
  "wbProvenanceTint",
  "wbExpressionMark",
];

const insertBlock = (
  editor: Editor,
  range: SlashRange,
  apply: (chain: ReturnType<Editor["chain"]>) => ReturnType<Editor["chain"]>,
): void => {
  apply(editor.chain().focus().deleteRange(range)).run();
};

/** The full block-insert registry, in menu order. */
export const SLASH_COMMANDS: readonly SlashCommand[] = [
  {
    id: "text",
    title: "Text",
    hint: "Plain paragraph",
    aliases: ["paragraph", "body", "p"],
    group: "basic",
    nodeName: "paragraph",
    run: (editor, range) => insertBlock(editor, range, (c) => c.setParagraph()),
  },
  {
    id: "heading-1",
    title: "Heading 1",
    hint: "Large section heading",
    aliases: ["h1", "title", "#"],
    group: "basic",
    nodeName: "heading",
    run: (editor, range) =>
      insertBlock(editor, range, (c) => c.toggleHeading({ level: 1 })),
  },
  {
    id: "heading-2",
    title: "Heading 2",
    hint: "Medium section heading",
    aliases: ["h2", "subtitle", "##"],
    group: "basic",
    nodeName: "heading",
    run: (editor, range) =>
      insertBlock(editor, range, (c) => c.toggleHeading({ level: 2 })),
  },
  {
    id: "heading-3",
    title: "Heading 3",
    hint: "Small section heading",
    aliases: ["h3", "###"],
    group: "basic",
    nodeName: "heading",
    run: (editor, range) =>
      insertBlock(editor, range, (c) => c.toggleHeading({ level: 3 })),
  },
  {
    id: "bullet-list",
    title: "Bullet list",
    hint: "An unordered list",
    aliases: ["ul", "unordered", "bullets", "-"],
    group: "lists",
    nodeName: "bulletList",
    run: (editor, range) =>
      insertBlock(editor, range, (c) => c.toggleBulletList()),
  },
  {
    id: "ordered-list",
    title: "Numbered list",
    hint: "An ordered list",
    aliases: ["ol", "ordered", "numbers", "1."],
    group: "lists",
    nodeName: "orderedList",
    run: (editor, range) =>
      insertBlock(editor, range, (c) => c.toggleOrderedList()),
  },
  {
    id: "task-list",
    title: "Task list",
    hint: "A checklist with checkboxes",
    aliases: ["todo", "checkbox", "checklist", "[]"],
    group: "lists",
    nodeName: "taskList",
    run: (editor, range) =>
      insertBlock(editor, range, (c) => c.toggleTaskList()),
  },
  {
    id: "quote",
    title: "Quote",
    hint: "A block quotation",
    aliases: ["blockquote", "citation", ">"],
    group: "blocks",
    nodeName: "blockquote",
    run: (editor, range) =>
      insertBlock(editor, range, (c) => c.toggleBlockquote()),
  },
  {
    id: "code-block",
    title: "Code block",
    hint: "A fenced code block",
    aliases: ["code", "fence", "pre", "```"],
    group: "blocks",
    nodeName: "codeBlock",
    run: (editor, range) =>
      insertBlock(editor, range, (c) => c.toggleCodeBlock()),
  },
  {
    id: "divider",
    title: "Divider",
    hint: "A horizontal rule",
    aliases: ["hr", "rule", "separator", "---"],
    group: "blocks",
    nodeName: "horizontalRule",
    run: (editor, range) =>
      insertBlock(editor, range, (c) => c.setHorizontalRule()),
  },
  {
    id: "table",
    title: "Table",
    hint: "A three-by-three table",
    aliases: ["grid", "rows", "columns"],
    group: "blocks",
    nodeName: "table",
    run: (editor, range) =>
      insertBlock(editor, range, (c) =>
        c.insertTable({ rows: 3, cols: 3, withHeaderRow: true }),
      ),
  },
  {
    id: "image",
    title: "Image",
    hint: "Insert an image placeholder",
    aliases: ["picture", "photo", "figure"],
    group: "media",
    nodeName: "image",
    run: (editor, range) =>
      insertBlock(editor, range, (c) => c.setImage({ src: "" })),
  },
];

/**
 * Filter the registry by a slash query, matched case-insensitively against the title and
 * the aliases as substrings. An empty query returns the whole registry in menu order, so
 * the popup opens showing every block type.
 */
export function filterSlashCommands(query: string): SlashCommand[] {
  const needle = query.trim().toLowerCase();
  if (needle.length === 0) return [...SLASH_COMMANDS];
  return SLASH_COMMANDS.filter((command) => {
    if (command.title.toLowerCase().includes(needle)) return true;
    return command.aliases.some((alias) =>
      alias.toLowerCase().includes(needle),
    );
  });
}

/** The commands grouped for rendering, dropping empty groups, in group order. */
export function groupSlashCommands(
  commands: readonly SlashCommand[],
): { readonly group: SlashCommandGroup; readonly commands: SlashCommand[] }[] {
  return SLASH_GROUP_ORDER.map((group) => ({
    group,
    commands: commands.filter((command) => command.group === group),
  })).filter((section) => section.commands.length > 0);
}

/**
 * Move the active index within the current item list, clamped and wrapping. Extracted as a
 * pure helper so the keyboard walk is testable without a live editor or popup.
 */
export function moveActiveIndex(
  current: number,
  count: number,
  delta: number,
): number {
  if (count === 0) return 0;
  return (current + delta + count) % count;
}
