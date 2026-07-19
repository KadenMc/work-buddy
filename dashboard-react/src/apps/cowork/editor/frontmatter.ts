/**
 * YAML frontmatter strip-and-reattach boundary (SP-3 case 3, gate condition 7).
 *
 * marked has no frontmatter concept: fed through the Markdown serializer, a `---` block
 * lexes as a thematic break, its YAML keys become paragraphs, underscores get escaped,
 * and on real files the backslash count grows without bound across passes. The fix,
 * proven byte-perfect on the SP-3 corpus, is to split the frontmatter off BEFORE parse
 * and re-attach it VERBATIM on serialize, never routing it through the serializer.
 *
 * The split is intentionally conservative: it recognizes frontmatter only when the file
 * opens with a `---` fence AND a matching closing `---` line exists, so a leading
 * thematic break is never mistaken for frontmatter.
 */

const FRONTMATTER_BOUNDARY =
  /^(---[ \t]*\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n)?)([\s\S]*)$/;

export interface FrontmatterSplit {
  /** The frontmatter block including both `---` fences and the trailing newline, or null. */
  readonly frontmatter: string | null;
  /** Everything after the frontmatter block. Equals the whole source when none is present. */
  readonly body: string;
}

/**
 * Split a Markdown source into its verbatim frontmatter block and its body. The
 * invariant `(frontmatter ?? "") + body === source` always holds, so the split is
 * lossless and reversible.
 */
export const splitFrontmatter = (source: string): FrontmatterSplit => {
  const match = FRONTMATTER_BOUNDARY.exec(source);
  if (match === null) {
    return { frontmatter: null, body: source };
  }
  const [, frontmatter, body] = match;
  return { frontmatter: frontmatter ?? null, body: body ?? "" };
};

/**
 * Re-attach a frontmatter block ahead of a serialized body, byte-for-byte. Passing a
 * null frontmatter returns the body unchanged.
 */
export const reattachFrontmatter = (
  frontmatter: string | null,
  body: string,
): string => (frontmatter === null ? body : `${frontmatter}${body}`);
