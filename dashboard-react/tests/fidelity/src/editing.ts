// Structured-edit helpers for the fidelity suite. These simulate the editor
// mutating one block's structured form and re-serializing ONLY that block, which
// is exactly the dirty-block path the block-splice materializer takes. They keep
// the suite's edit simulation deterministic and out of the pure materializer.
import {
  serializeBlockJson,
  type Block,
  type PmDoc,
  type PmNode,
} from "./materializer.js";
import { sharedManager, type FidelityManager } from "./bundle.js";

/** Deep clone a ProseMirror JSON doc (structuredClone is available in Node 18+). */
export function clonePm(doc: PmDoc): PmDoc {
  return structuredClone(doc);
}

/** Append text into the first text-bearing leaf of a structured block. Returns
 *  true when a text node was found and edited. */
export function appendTextToFirstLeaf(node: PmNode, suffix: string): boolean {
  if (node.type === "text" && typeof node.text === "string") {
    node.text = node.text + suffix;
    return true;
  }
  if (Array.isArray(node.content)) {
    for (const child of node.content) {
      if (appendTextToFirstLeaf(child, suffix)) {
        return true;
      }
    }
  }
  return false;
}

/** Simulate editing one block: clone its structured form, append a text marker,
 *  and re-serialize just that block. Returns the edited Markdown, or null when
 *  the block carries no structured form (a separator) or no text leaf. */
export function simulateBlockEdit(
  block: Block,
  marker = " [EDITED]",
  mm: FidelityManager = sharedManager(),
): string | null {
  if (block.json === null) {
    return null;
  }
  const edited = clonePm(block.json);
  const changed = appendTextToFirstLeaf(edited as PmNode, marker);
  if (!changed) {
    return null;
  }
  return serializeBlockJson(edited, mm);
}
