// The frontmatter strip-and-reattach boundary (C1 surface contract section 7.2
// fail-hard rule 5, SP-3 case 3). YAML frontmatter is split off before the body
// is ever lexed or serialized, and re-attached byte-for-byte on materialize.
// Feeding frontmatter through @tiptap/markdown destroys the YAML and, on real
// files, grows escaping without bound (SP-3 measured 55 to 117 backslashes over
// six passes on artifact-system.md), so the boundary is mandatory.

// A leading fenced YAML block: opens with `---` on its own line and closes with
// the FIRST later `---` line. The optional trailing newline is captured so the
// frontmatter plus the body reconstruct the source with no gap.
const FRONTMATTER_RE = /^(---\n[\s\S]*?\n---\n?)([\s\S]*)$/;

export interface SplitDocument {
  /** Verbatim frontmatter block including its delimiters, or "" when absent. */
  frontmatter: string;
  /** Everything after the frontmatter block (the whole source when absent). */
  body: string;
}

/** Split a source string into verbatim frontmatter and body. The two pieces
 *  always concatenate back to the input exactly: frontmatter + body === source. */
export function splitFrontmatter(source: string): SplitDocument {
  const match = source.match(FRONTMATTER_RE);
  if (match) {
    return { frontmatter: match[1], body: match[2] };
  }
  return { frontmatter: "", body: source };
}

/** Re-attach a verbatim frontmatter block to a body. Inverse of the split. */
export function reattachFrontmatter(frontmatter: string, body: string): string {
  return frontmatter + body;
}

/** True when the source carries a leading YAML frontmatter block. */
export function hasFrontmatter(source: string): boolean {
  return FRONTMATTER_RE.test(source);
}
