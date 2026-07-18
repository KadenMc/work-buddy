// The frontmatter strip-and-reattach boundary (C1 surface contract section 7.2
// fail-hard rule 5, SP-3 case 3). YAML frontmatter is split off before the body
// is ever lexed or serialized, and re-attached byte-for-byte on materialize.
// Feeding frontmatter through @tiptap/markdown destroys the YAML and, on real
// files, grows escaping without bound (SP-3 measured 55 to 117 backslashes over
// six passes on artifact-system.md), so the boundary is mandatory.
//
// The gate now validates the SHIPPED boundary, not a harness-local copy (S6): it
// delegates to the production splitFrontmatter / reattachFrontmatter in
// src/apps/cowork/editor, which is the same pair the production import path
// (markdownImport.ts) uses, so a green gate guards the code that actually strips
// and re-attaches frontmatter on materialize. The production split returns a
// nullable frontmatter (null when absent) and this harness shape uses "" for
// absent, so the adapter coerces null to "".
import {
  reattachFrontmatter as productionReattach,
  splitFrontmatter as productionSplit,
} from "../../../src/apps/cowork/editor/frontmatter.js";

export interface SplitDocument {
  /** Verbatim frontmatter block including its delimiters, or "" when absent. */
  frontmatter: string;
  /** Everything after the frontmatter block (the whole source when absent). */
  body: string;
}

/** Split a source string into verbatim frontmatter and body. The two pieces
 *  always concatenate back to the input exactly: frontmatter + body === source. */
export function splitFrontmatter(source: string): SplitDocument {
  const { frontmatter, body } = productionSplit(source);
  return { frontmatter: frontmatter ?? "", body };
}

/** Re-attach a verbatim frontmatter block to a body. Inverse of the split. */
export function reattachFrontmatter(frontmatter: string, body: string): string {
  return productionReattach(frontmatter, body);
}

/** True when the source carries a leading YAML frontmatter block. */
export function hasFrontmatter(source: string): boolean {
  return productionSplit(source).frontmatter !== null;
}
