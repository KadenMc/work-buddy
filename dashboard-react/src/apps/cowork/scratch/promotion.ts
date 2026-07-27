import type { CoworkDocumentSummary } from "../contracts";

export type ScratchPromotionResult =
  | { readonly retired: true }
  | { readonly retired: false; readonly error: unknown };

/**
 * Promotion has one intentionally strict commit boundary: the registered document must
 * open successfully before the browser-local scratch can leave the recovery list. Once
 * that boundary is crossed, a cleanup failure is a recoverable duplicate, not a failed
 * document creation.
 */
export const finishScratchPromotion = async (
  document: CoworkDocumentSummary,
  scratchId: string,
  openDocument: (document: CoworkDocumentSummary) => Promise<void>,
  retireScratch: (scratchId: string) => Promise<void>,
): Promise<ScratchPromotionResult> => {
  await openDocument(document);
  try {
    await retireScratch(scratchId);
    return { retired: true };
  } catch (error) {
    return { retired: false, error };
  }
};
