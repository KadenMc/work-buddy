import type { CoworkDocumentSummary } from "../contracts";

/**
 * Promotion has one intentionally strict commit boundary: the registered document must
 * open successfully before the device-local scratch can leave the recovery list.
 */
export const finishScratchPromotion = async (
  document: CoworkDocumentSummary,
  scratchId: string,
  openDocument: (document: CoworkDocumentSummary) => Promise<void>,
  retireScratch: (scratchId: string) => Promise<void>,
): Promise<void> => {
  await openDocument(document);
  await retireScratch(scratchId);
};
