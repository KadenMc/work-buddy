import type { Editor } from "@tiptap/core";

import type { ScrollAnchorTarget } from "../chat";
import type { ReviewAnchorController } from "../rail/provider";
import { resolveProvenanceQuoteAnchorDetailed } from "../suggestions/anchor";

import type { TruthPassageConnection } from "./contracts";

/** Explicit navigation remains available when terminal claims have no persistent mark. */
export function revealTruthPassage(
  editor: Editor | null,
  connection: TruthPassageConnection,
  anchors: ReviewAnchorController,
  revealTemporary: (target: ScrollAnchorTarget) => boolean,
): boolean {
  if (editor === null || editor.isDestroyed) return false;
  const mark = [...editor.view.dom.querySelectorAll<HTMLElement>("[data-wb-expression-id]")]
    .find((element) => element.dataset.wbExpressionId === connection.expressionId);
  if (mark !== undefined) {
    anchors.revealAnchor(connection.expressionId, "expression", { flash: true });
    return true;
  }
  const range = resolveProvenanceQuoteAnchorDetailed(editor.state.doc, connection.selector);
  if (range.state !== "unique") return false;
  return revealTemporary({
    spanId: `truth-passage:${connection.expressionId}`,
    anchor: {
      exact: connection.selector.exact,
      prefix: connection.selector.prefix,
      suffix: connection.selector.suffix,
    },
  });
}
