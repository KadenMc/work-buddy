// The frozen Markdown extension bundle, restricted to the schema-node subset that
// projection fidelity depends on. C1 surface contract section 6 pins the full
// editor bundle at 3.28.0. The editor and collaboration extensions (Collaboration,
// y-tiptap, UniqueID, Link, suggestion) do not change the Markdown schema, so the
// fidelity gate loads only StarterKit plus the three constructs the corpus needs
// as first-class nodes (Table, TaskList, Image), which are the section 7.2 minimum.
import { MarkdownManager } from "@tiptap/markdown";
import StarterKit from "@tiptap/starter-kit";
import { Table, TableRow, TableHeader, TableCell } from "@tiptap/extension-table";
import { TaskList, TaskItem } from "@tiptap/extension-list";
import Image from "@tiptap/extension-image";

/** The optional (non-StarterKit) extensions this suite gates schema coverage on.
 *  Each id maps to the ProseMirror node types the extension contributes. */
export const OPTIONAL_EXTENSION_NODES = {
  table: ["table", "tableRow", "tableHeader", "tableCell"],
  taskList: ["taskList", "taskItem"],
  image: ["image"],
} as const;

export type OptionalExtensionId = keyof typeof OPTIONAL_EXTENSION_NODES;

export const OPTIONAL_EXTENSION_IDS: OptionalExtensionId[] = [
  "table",
  "taskList",
  "image",
];

const EXTENSION_GROUPS: Record<OptionalExtensionId, unknown[]> = {
  table: [Table, TableRow, TableHeader, TableCell],
  taskList: [TaskList, TaskItem],
  image: [Image],
};

// A minimal structural type for the marked instance MarkdownManager exposes as
// `.instance`. The block-splice materializer lexes through this exact instance so
// its token stream matches the manager's own parse (SP-3 case 4 mechanism).
export interface MarkedToken {
  type: string;
  raw: string;
}

export interface MarkedInstance {
  Lexer: new (options: unknown) => { lex(source: string): MarkedToken[] };
  defaults: unknown;
}

// The manager surface the harness relies on. @tiptap/markdown ships its own types,
// but `.instance` (the marked engine) is not in the public type, so we widen it here.
export interface FidelityManager {
  parse(markdown: string): { type: string; content?: unknown[] };
  serialize(doc: unknown): string;
  instance: MarkedInstance;
}

/** Build a MarkdownManager over the full fidelity bundle, or over the bundle with
 *  the named optional extensions omitted (the negative control for schema-node
 *  data-loss detection, C1 fail-hard rule 3). */
export function createManager(
  omit: OptionalExtensionId[] = [],
): FidelityManager {
  const extensions: unknown[] = [StarterKit];
  for (const id of OPTIONAL_EXTENSION_IDS) {
    if (!omit.includes(id)) {
      extensions.push(...EXTENSION_GROUPS[id]);
    }
  }
  return new MarkdownManager({
    extensions: extensions as never,
  }) as unknown as FidelityManager;
}

/** The set of node types the full frozen fidelity bundle can represent. Used to
 *  assert that every extension a corpus file requires is actually covered. */
export function supportedOptionalNodeTypes(): Set<string> {
  const types = new Set<string>();
  for (const id of OPTIONAL_EXTENSION_IDS) {
    for (const node of OPTIONAL_EXTENSION_NODES[id]) {
      types.add(node);
    }
  }
  return types;
}

let shared: FidelityManager | null = null;

/** A process-wide manager over the full bundle (managers are reusable and pure). */
export function sharedManager(): FidelityManager {
  if (shared === null) {
    shared = createManager();
  }
  return shared;
}
