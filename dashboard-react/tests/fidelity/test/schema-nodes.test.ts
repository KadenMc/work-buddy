// Fail-hard rule 3 (C1 surface contract section 7.2, SP-3 finding 3): the suite
// MUST fail if any corpus construct lacks a schema node, never silently drop it.
// The frozen bundle carries Table, TaskList, and Image at minimum. This proves the
// full bundle covers every construct the corpus needs, and via a negative control
// (a manager missing one extension) demonstrates the detector catches a dropped
// construct as a hard failure, matching SP-3's empty-table data-loss finding.
import { describe, it, expect } from "vitest";
import { readCorpus } from "../src/corpus.js";
import {
  createManager,
  OPTIONAL_EXTENSION_NODES,
  OPTIONAL_EXTENSION_IDS,
  type OptionalExtensionId,
  type FidelityManager,
} from "../src/bundle.js";
import { splitFrontmatter } from "../src/frontmatter.js";

const corpus = readCorpus();
const fullManager = createManager();

interface PmLike {
  type?: string;
  content?: PmLike[];
}

function collectNodeTypes(node: PmLike, into: Set<string>): Set<string> {
  if (typeof node.type === "string") into.add(node.type);
  if (Array.isArray(node.content)) {
    for (const child of node.content) collectNodeTypes(child, into);
  }
  return into;
}

function nodeTypesOf(manager: FidelityManager, body: string): Set<string> {
  return collectNodeTypes(manager.parse(body) as PmLike, new Set());
}

/** The required extensions whose node types are absent when `body` is parsed by
 *  `manager`, that is, constructs that silently dropped for lack of a schema node. */
function missingSchemaNodes(
  manager: FidelityManager,
  body: string,
  requiredExtensions: string[],
): string[] {
  const present = nodeTypesOf(manager, body);
  const missing: string[] = [];
  for (const ext of requiredExtensions) {
    const nodeTypes = OPTIONAL_EXTENSION_NODES[ext as OptionalExtensionId] ?? [];
    if (!nodeTypes.some((type) => present.has(type))) missing.push(ext);
  }
  return missing;
}

describe("rule 3: schema-missing-node hard failure", () => {
  it("the frozen bundle covers every construct the corpus requires", () => {
    for (const file of corpus) {
      const { body } = splitFrontmatter(file.source);
      const missing = missingSchemaNodes(
        fullManager,
        body,
        file.entry.required_extensions,
      );
      expect(missing, `${file.entry.path} dropped ${missing.join(", ")}`).toEqual(
        [],
      );
    }
  });

  it("detects a dropped construct when an extension is missing (negative control)", () => {
    const corpusExtensions = new Set<string>();
    for (const file of corpus) {
      for (const ext of file.entry.required_extensions) corpusExtensions.add(ext);
    }
    // Every construct the corpus uses must be catchable when its schema node is gone.
    for (const ext of corpusExtensions) {
      const file = corpus.find((f) =>
        f.entry.required_extensions.includes(ext),
      );
      expect(file, `no corpus file requires ${ext}`).toBeDefined();
      if (!file) continue;
      const { body } = splitFrontmatter(file.source);
      const crippled = createManager([ext as OptionalExtensionId]);
      const missing = missingSchemaNodes(crippled, body, [ext]);
      // The detector fires: this is the hard-failure signal.
      expect(missing, `missing ${ext} not detected`).toContain(ext);
    }
  });

  it("a table silently drops to empty without the Table extension (SP-3 data loss)", () => {
    const tableSource = "| a | b |\n| - | - |\n| 1 | 2 |";
    const withTable = fullManager.serialize(fullManager.parse(tableSource));
    const withoutTable = createManager(["table"]);
    const dropped = withoutTable.serialize(withoutTable.parse(tableSource));
    expect(withTable.includes("|")).toBe(true);
    expect(dropped.includes("|")).toBe(false);
    expect(dropped.trim()).toBe("");
  });

  it("the negative control can omit each optional extension independently", () => {
    for (const id of OPTIONAL_EXTENSION_IDS) {
      const manager = createManager([id]);
      const types = nodeTypesOf(
        manager,
        "| a | b |\n| - | - |\n| 1 | 2 |\n\n- [ ] task\n\n![alt](x.png)",
      );
      for (const nodeType of OPTIONAL_EXTENSION_NODES[id]) {
        expect(types.has(nodeType), `${id} node ${nodeType} leaked`).toBe(false);
      }
    }
  });
});
