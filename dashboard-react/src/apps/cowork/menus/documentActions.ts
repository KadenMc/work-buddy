import type { Editor } from "@tiptap/core"
import type { Node as ProseMirrorNode } from "@tiptap/pm/model"
import type { CoworkEditorLens } from "../editor/ledgerDecorations"
import { coworkExpressionTargets, coworkLedgerDecorationsKey } from "../editor/ledgerDecorations"
import { quoteAnchorFromRange, type RangeQuoteAnchor } from "../feedback/feedbackAnchor"
import { resolveQuoteAnchor } from "../suggestions/anchor"
import type { TruthEditorIntegration } from "../truth/contracts"
import type { TruthStore } from "../truth/store"
import type { CoworkShortcutBindings } from "../keyboard"

export interface CoworkDocumentActions {
  readonly active?: boolean
  readonly truthStore?: TruthStore | null
  readonly truthEditor?: TruthEditorIntegration
  readonly onOpenClaim?: (claimId: string) => void
  readonly onAsk?: () => void | Promise<void>
  readonly onSetWorkingTarget?: () => void
  readonly onOpenSuggestion?: (proposalId: string) => void
  readonly shortcutBindings?: CoworkShortcutBindings
}

export interface CoworkPassageTarget {
  readonly doc: ProseMirrorNode
  readonly from: number
  readonly to: number
  readonly selected: boolean
  readonly anchor: RangeQuoteAnchor
  readonly claims: readonly { readonly id: string; readonly proposition: string; readonly status: string | null }[]
  readonly proposals: readonly string[]
}

/** A pointer uses the selection only when it points inside that selection. */
export function resolveCoworkPassageTarget(editor: Editor, position?: number): CoworkPassageTarget | null {
  if (editor.isDestroyed || (position !== undefined && !Number.isFinite(position))) return null
  const { doc, selection } = editor.state
  const at = Math.max(0, Math.min(position ?? selection.from, doc.content.size))
  const selected = !selection.empty && (position === undefined || (at >= selection.from && at <= selection.to))
  const resolved = doc.resolve(at)
  if (!selected && !resolved.parent.isTextblock) return null
  const from = selected ? selection.from : resolved.start()
  const to = selected ? selection.to : resolved.end()
  const anchor = quoteAnchorFromRange(doc, from, to)
  if (anchor === null) return null
  const touches = (range: { from: number; to: number }) => selected
    ? range.from < to && range.to > from
    : range.from <= at && range.to >= at
  const claims = new Map<string, CoworkPassageTarget["claims"][number]>()
  for (const expression of coworkExpressionTargets(editor, { includeTerminal: true }).filter(touches)) {
    for (const id of expression.claimIds) claims.set(id, {
      id, proposition: expression.proposition || "Claim recorded at this passage", status: expression.claimStatus,
    })
  }
  const projection = coworkLedgerDecorationsKey.getState(editor.state)?.projection
  const proposals = [...projection?.edits ?? [], ...projection?.flags ?? []].flatMap((item) => {
    const range = resolveQuoteAnchor(doc, item.quoteAnchor)
    return range !== null && touches(range) ? [item.proposalId] : []
  })
  return { doc, from, to, selected, anchor, claims: [...claims.values()], proposals: [...new Set(proposals)] }
}

export function selectCoworkPassageTarget(editor: Editor, target: CoworkPassageTarget): void {
  if (editor.isDestroyed || !editor.state.doc.eq(target.doc)) throw new Error("The passage changed. Open its actions again.")
  editor.commands.setTextSelection({ from: target.from, to: target.to })
}

export interface CoworkDocumentActionItem {
  readonly id: string
  readonly label: string
  readonly description: string
  readonly blockedReason: string | null
  readonly run: () => void | Promise<void>
}

export function coworkTruthMenuItems(
  lens: CoworkEditorLens,
  target: CoworkPassageTarget,
  actions: CoworkDocumentActions,
  prepare: () => void,
  options: { readonly readOnly?: boolean } = {},
): CoworkDocumentActionItem[] {
  const items: CoworkDocumentActionItem[] = []
  const readOnlyReason = options.readOnly ? "This document is read-only." : null
  const truthUnavailable = actions.onOpenClaim === undefined ? "Truth is unavailable for this document." : null
  const runGuarded = (reason: () => string | null, run: () => void | Promise<void>) => async () => {
    const blocked = reason()
    if (blocked !== null) throw new Error(blocked)
    await run()
  }
  for (const claim of target.claims) {
    items.push({
      id: `claim:${claim.id}`, label: "Show claim", description: claim.proposition,
      blockedReason: truthUnavailable,
      run: runGuarded(() => truthUnavailable, () => actions.onOpenClaim?.(claim.id)),
    })
    if (lens === "truth") for (const action of ["confirm", "reject"] as const) {
      const decisionReason = readOnlyReason ?? truthUnavailable ?? (claim.status !== "proposed"
        ? "This decision is available for proposed claims."
        : actions.truthStore == null ? "Truth is unavailable for this document." : null)
      items.push({
        id: `${action}:${claim.id}`, label: action === "confirm" ? "Confirm claim…" : "Reject claim…",
        description: claim.proposition,
        blockedReason: decisionReason,
        run: runGuarded(() => decisionReason, () => {
          actions.onOpenClaim?.(claim.id)
          actions.truthStore?.requestDecision(claim.id, action)
        }),
      })
    }
  }
  if (lens === "truth" && target.claims.length === 0) {
    const selectionReason = target.selected ? null : "Select the passage first."
    const blockedReason = (kind: "analyze" | "manual") => {
      const controller = actions.truthStore?.getActions()
      const captureAvailable = kind === "analyze" ? actions.truthEditor?.captureAnalysisTarget : actions.truthEditor?.captureSelection
      return readOnlyReason ?? selectionReason ?? (controller == null ? "Truth actions are preparing." : null)
        ?? (captureAvailable === undefined ? "The editor cannot capture a passage right now." : null)
        ?? (kind === "analyze" ? controller?.analyzeBlockedReason : controller?.manualBlockedReason) ?? null
    }
    items.push({
      id: "analyze", label: "Analyze passage", description: "Prepare claims from this selection for review.",
      blockedReason: blockedReason("analyze"),
      run: runGuarded(() => blockedReason("analyze"), async () => {
        prepare()
        const capture = await actions.truthEditor?.captureAnalysisTarget?.("current_selection")
        if (capture === undefined) throw new Error("Analysis capture is unavailable for this document.")
        const blocked = blockedReason("analyze")
        if (blocked !== null) throw new Error(blocked)
        actions.truthStore?.getActions()?.analyzePassage(capture)
      }),
    })
    for (const mode of ["connect", "propose"] as const) items.push({
      id: mode, label: mode === "connect" ? "Connect to existing claim…" : "Add claim…",
      description: "Use this exact selected passage.",
      blockedReason: blockedReason("manual"),
      run: runGuarded(() => blockedReason("manual"), async () => {
        prepare()
        const capture = await actions.truthEditor?.captureSelection()
        if (capture === undefined) throw new Error("Passage capture is unavailable for this document.")
        const blocked = blockedReason("manual")
        if (blocked !== null) throw new Error(blocked)
        actions.truthStore?.getActions()?.openComposer(mode, capture)
      }),
    })
  }
  if (lens === "review") for (const id of target.proposals) items.push({
    id: `proposal:${id}`, label: "Open this suggestion", description: "Review the suggestion at this passage.",
    blockedReason: actions.onOpenSuggestion === undefined ? "Review is unavailable for this document." : null,
    run: () => actions.onOpenSuggestion?.(id),
  })
  return items
}
