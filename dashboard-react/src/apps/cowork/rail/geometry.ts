/**
 * Pure geometry for the aligned margin-card stream (SP-6 variant A, audit A12).
 * The layout math is separated from the DOM so it can be unit-tested and run
 * outside the React render cycle. Real per-anchor alignment is not a CSS
 * freebie, and cards overlap when anchors cluster on adjacent lines, so this
 * greedy resolver keeps every card as close to its anchor as it can while
 * guaranteeing a minimum gap between neighbours.
 */

/** One card to place, in document order, with its anchor top and measured height. */
export interface AlignInput {
  readonly id: string;
  /** The top offset of the card's anchor, in the stream scroll coordinate space. */
  readonly anchorTop: number;
  /** The measured height of the card. */
  readonly height: number;
}

/** The resolved top offset for one card. */
export interface AlignPlacement {
  readonly id: string;
  readonly top: number;
}

export interface AlignOptions {
  /** Minimum vertical gap between two stacked cards. Defaults to 8. */
  readonly gap?: number;
  /** The smallest top a card may take. Defaults to 0. */
  readonly minTop?: number;
  /**
   * Maximum empty space before the first card. When present, the whole aligned
   * stream shifts upward by any excess while preserving relative anchor spacing.
   */
  readonly maxLeadingSpace?: number;
}

/**
 * Place cards next to their anchors, resolving overlap by pushing a clustered
 * card down to just below its predecessor. Input is assumed in document order,
 * but it is sorted defensively by anchorTop so ordering is never load-bearing on
 * the caller. When maxLeadingSpace is set, the resolver first removes the same
 * excess leading offset from every anchor. Each card then sits at max(its
 * adjusted anchor top, previous card's bottom plus the gap), which preserves
 * document order and relative anchor spacing while minimizing collision drift.
 */
export function computeAlignedLayout(
  inputs: readonly AlignInput[],
  options: AlignOptions = {},
): AlignPlacement[] {
  const gap = options.gap ?? 8;
  const minTop = options.minTop ?? 0;
  const ordered = [...inputs].sort((a, b) => a.anchorTop - b.anchorTop);
  const maxLeadingSpace =
    options.maxLeadingSpace === undefined
      ? undefined
      : Math.max(0, options.maxLeadingSpace);
  const naturalFirstTop = Math.max(ordered[0]?.anchorTop ?? minTop, minTop);
  const alignmentOffset =
    maxLeadingSpace === undefined
      ? 0
      : Math.max(0, naturalFirstTop - (minTop + maxLeadingSpace));

  const placements: AlignPlacement[] = [];
  let cursor = minTop;
  for (const input of ordered) {
    const top = Math.max(input.anchorTop - alignmentOffset, cursor, minTop);
    placements.push({ id: input.id, top });
    cursor = top + input.height + gap;
  }
  return placements;
}

/**
 * Whether two placement lists are equal, so the imperative layout writer can
 * skip a DOM write when nothing moved. Order-sensitive, matching the resolver
 * output.
 */
export function placementsEqual(
  a: readonly AlignPlacement[],
  b: readonly AlignPlacement[],
): boolean {
  if (a.length !== b.length) return false;
  for (let index = 0; index < a.length; index += 1) {
    if (a[index].id !== b[index].id) return false;
    if (a[index].top !== b[index].top) return false;
  }
  return true;
}
