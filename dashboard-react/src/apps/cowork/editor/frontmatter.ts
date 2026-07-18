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
 * thematic break is never mistaken for frontmatter. A leading UTF-8 BOM is split off
 * before the fence test (the BOM code point would otherwise defeat the `^---` anchor and
 * route the whole `---` block through the serializer) and carried verbatim on the
 * frontmatter side, so the split stays lossless.
 */

const BOM = "\uFEFF";

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
 * lossless and reversible. A leading BOM is preserved on the frontmatter side when
 * frontmatter is present, otherwise it stays at the head of the body.
 */
export const splitFrontmatter = (source: string): FrontmatterSplit => {
  const leadingBom = source.startsWith(BOM) ? BOM : "";
  const rest = leadingBom.length > 0 ? source.slice(leadingBom.length) : source;
  const match = FRONTMATTER_BOUNDARY.exec(rest);
  if (match === null) {
    return { frontmatter: null, body: source };
  }
  const [, frontmatter, body] = match;
  return { frontmatter: leadingBom + (frontmatter ?? ""), body: body ?? "" };
};

/**
 * Re-attach a frontmatter block ahead of a serialized body, byte-for-byte. Passing a
 * null frontmatter returns the body unchanged.
 */
export const reattachFrontmatter = (
  frontmatter: string | null,
  body: string,
): string => (frontmatter === null ? body : `${frontmatter}${body}`);
