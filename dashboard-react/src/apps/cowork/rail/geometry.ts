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
}

/**
 * Place cards next to their anchors, resolving overlap by pushing a clustered
 * card down to just below its predecessor. Input is assumed in document order,
 * but it is sorted defensively by anchorTop so ordering is never load-bearing on
 * the caller. Each card sits at max(its anchor top, previous card's bottom plus
 * the gap), which preserves document order and minimizes the drift from each
 * anchor.
 */
export function computeAlignedLayout(
  inputs: readonly AlignInput[],
  options: AlignOptions = {},
): AlignPlacement[] {
  const gap = options.gap ?? 8;
  const minTop = options.minTop ?? 0;
  const ordered = [...inputs].sort((a, b) => a.anchorTop - b.anchorTop);

  const placements: AlignPlacement[] = [];
  let cursor = minTop;
  for (const input of ordered) {
    const top = Math.max(input.anchorTop, cursor, minTop);
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
