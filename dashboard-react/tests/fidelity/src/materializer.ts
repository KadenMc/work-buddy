// Block-splice materializer: the REFERENCE IMPLEMENTATION of Co-work projection
// fidelity. The production materializer adopts this design (the harness is its
// executable spec, C1 surface contract section 7.2, SP-3 case 4 design sketch).
//
// The thesis, proven in SP-3 and re-verified across this corpus: @tiptap/markdown
// whole-document serialization normalizes every file (0 of 32 round-trip
// byte-identical). But marked lexer tokens each carry exact source bytes in
// `raw`, and concatenating every token raw reconstructs the body byte-for-byte
// (verified 32 of 32 here). So materialization splices per top-level block:
// UNEDITED blocks are copied byte-verbatim from the source, and only edited
// blocks are re-serialized. A one-block edit yields a one-block diff, and every
// normalization and corruption is confined to the block the human actually
// changed. Frontmatter is stripped and re-attached verbatim at the boundary.
import { splitFrontmatter } from "./frontmatter.js";
import { sha256, lf } from "./text.js";
import { sharedManager, type FidelityManager } from "./bundle.js";

/** Minimal ProseMirror JSON node shape (structural, not exhaustive). */
export interface PmNode {
  type: string;
  content?: PmNode[];
  text?: string;
  attrs?: Record<string, unknown>;
  marks?: unknown[];
}

export interface PmDoc {
  type: string;
  content?: PmNode[];
}

/** Obsidian bracket constructs that are NOT first-class schema nodes in v1
 *  (C1 surface section 6, gate condition 9). Feeding an EDITED block that holds
 *  one through the serializer corrupts it (wikilinks and callouts get their
 *  brackets backslash-escaped, SP-3 case 2), so such an edited block is FLAGGED
 *  rather than serialized. Inside UNEDITED blocks they are copied byte-verbatim. */
export const UNKNOWN_CONSTRUCT_PATTERNS: { label: string; test: RegExp }[] = [
  { label: "embed", test: /!\[\[[^\]]+\]\]/ },
  { label: "wikilink", test: /(?<!!)\[\[[^\]]+\]\]/ },
  { label: "callout", test: /^[ \t]*>[ \t]*\[![^\]]+\]/m },
  { label: "footnote", test: /\[\^[^\]]+\]/ },
];

/** Detect Obsidian constructs with no first-class schema node in a raw block. */
export function detectUnknownConstructs(raw: string): string[] {
  const found: string[] = [];
  for (const { label, test } of UNKNOWN_CONSTRUCT_PATTERNS) {
    if (test.test(raw)) {
      found.push(label);
    }
  }
  return found;
}

/** One top-level block of a document, keyed to its exact source range. */
export interface Block {
  /** Stable working id (the production surface keys this off UniqueID). */
  id: string;
  /** The marked token type (heading, paragraph, table, space, ...). */
  type: string;
  /** False for inter-block separators (marked `space` tokens). */
  isContent: boolean;
  /** Exact source bytes for this block. Concatenating every block raw (content
   *  and separators, in order) reconstructs the body byte-for-byte. */
  raw: string;
  /** Body-relative start offset (inclusive). */
  start: number;
  /** Body-relative end offset (exclusive). */
  end: number;
  /** Parsed structured form (content blocks only), the editor working state. */
  json: PmDoc | null;
  /** SHA-256 of this block re-serialized, its clean-state fingerprint. */
  normHash: string | null;
  /** Non-first-class Obsidian constructs present in the raw source. */
  unknownConstructs: string[];
}

export interface ImportedDocument {
  /** Verbatim frontmatter block, or "" when absent. */
  frontmatter: string;
  /** The frontmatter-stripped body. */
  body: string;
  /** The LF-normalized full source. */
  source: string;
  /** Top-level blocks in document order, including separators. */
  blocks: Block[];
}

function lexBody(body: string, mm: FidelityManager) {
  const instance = mm.instance;
  const lexer = new instance.Lexer(instance.defaults);
  return lexer.lex(body);
}

/** Import a document: strip frontmatter, lex the body into top-level blocks with
 *  exact source ranges, and parse each content block to its structured form. */
export function importDocument(
  source: string,
  mm: FidelityManager = sharedManager(),
): ImportedDocument {
  const normalized = lf(source);
  const { frontmatter, body } = splitFrontmatter(normalized);
  const tokens = lexBody(body, mm);
  let offset = 0;
  const blocks: Block[] = tokens.map((token, index) => {
    const start = offset;
    offset += token.raw.length;
    const isContent = token.type !== "space";
    const json = isContent ? (mm.parse(token.raw) as PmDoc) : null;
    return {
      id: `blk-${index}`,
      type: token.type,
      isContent,
      raw: token.raw,
      start,
      end: offset,
      json,
      normHash: json ? sha256(mm.serialize(json)) : null,
      unknownConstructs: detectUnknownConstructs(token.raw),
    };
  });
  return { frontmatter, body, source: normalized, blocks };
}

/** Serialize one block's structured form back to Markdown (the dirty-block path).
 *  Only edited blocks take this path, so its normalizations never reach unedited
 *  regions. */
export function serializeBlockJson(
  json: PmDoc,
  mm: FidelityManager = sharedManager(),
): string {
  return mm.serialize(json);
}

export interface MaterializeOptions {
  /** When true, an edited block that holds an unknown construct is serialized
   *  anyway instead of kept verbatim. Default false: such blocks are flagged and
   *  their raw source is preserved (the edit-safety rule, gate condition 9). */
  serializeFlaggedBlocks?: boolean;
}

export interface MaterializeResult {
  /** The rendered Markdown: frontmatter, then spliced blocks. */
  markdown: string;
  /** Edited blocks that hold a non-first-class construct, never silently
   *  normalized (C1 fail-hard rule 4). */
  flaggedUnknowns: { blockId: string; constructs: string[] }[];
  /** Ids of the blocks that took the re-serialize path this materialize. */
  dirtyBlockIds: string[];
}

/** Materialize a document to Markdown. `edits` maps a block id to its already
 *  re-serialized Markdown (the client serializes edited blocks, C1 section 1.6).
 *  Unedited blocks are copied byte-verbatim. An edited block holding an unknown
 *  construct is flagged and kept verbatim unless `serializeFlaggedBlocks` is set. */
export function materialize(
  doc: ImportedDocument,
  edits: Map<string, string> = new Map(),
  options: MaterializeOptions = {},
): MaterializeResult {
  let markdown = doc.frontmatter;
  const flaggedUnknowns: { blockId: string; constructs: string[] }[] = [];
  const dirtyBlockIds: string[] = [];
  for (const block of doc.blocks) {
    const isEdited = block.isContent && edits.has(block.id);
    if (!isEdited) {
      markdown += block.raw;
      continue;
    }
    dirtyBlockIds.push(block.id);
    if (block.unknownConstructs.length > 0) {
      flaggedUnknowns.push({
        blockId: block.id,
        constructs: block.unknownConstructs,
      });
      if (options.serializeFlaggedBlocks !== true) {
        markdown += block.raw;
        continue;
      }
    }
    markdown += edits.get(block.id) as string;
  }
  return { markdown, flaggedUnknowns, dirtyBlockIds };
}

/** The content blocks of a document (separators excluded). */
export function contentBlocks(doc: ImportedDocument): Block[] {
  return doc.blocks.filter((block) => block.isContent);
}

/** Look up a block by id. */
export function blockById(
  doc: ImportedDocument,
  id: string,
): Block | undefined {
  return doc.blocks.find((block) => block.id === id);
}
