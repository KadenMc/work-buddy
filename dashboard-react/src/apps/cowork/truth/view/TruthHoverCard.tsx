import { useEffect, useLayoutEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import type { Editor } from "@tiptap/core"
import { coworkExpressionTargets, coworkTruthLegendCounts } from "../../editor/ledgerDecorations"

interface HoverClaim { readonly id: string; readonly proposition: string; readonly status: string; readonly evidenceCount: number }
interface HoverState {
  readonly claims: readonly HoverClaim[]
  readonly anchor: { readonly left: number; readonly top: number; readonly bottom: number }
  readonly left: number
  readonly top: number
  readonly width: number
  readonly maxHeight: number
  readonly positioned: boolean
}

const expandedSelectionTouches = (root: HTMLElement) => {
  const selection = document.getSelection()
  return selection !== null && !selection.isCollapsed &&
    ((selection.anchorNode !== null && root.contains(selection.anchorNode)) ||
      (selection.focusNode !== null && root.contains(selection.focusNode)))
}

export function TruthHoverCard({ editor, active, onOpenClaim }: {
  readonly editor: Editor | null
  readonly active: boolean
  readonly onOpenClaim: (id: string) => void
}) {
  const [hover, setHover] = useState<HoverState | null>(null)
  const card = useRef<HTMLElement>(null)
  const hideTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const cancelHide = () => { if (hideTimer.current !== null) clearTimeout(hideTimer.current) }
  useEffect(() => {
    if (!active || editor === null) { setHover(null); return }
    const root = editor.view.dom
    let current: HTMLElement | null = null
    const clear = () => { cancelHide(); current = null; setHover(null) }
    const show = (element: Element | null) => {
      if (!editor.state.selection.empty || expandedSelectionTouches(root)) { clear(); return }
      const mark = element?.closest<HTMLElement>("[data-wb-anchor-kind='expression']")
      if (!mark || !root.contains(mark)) { clear(); return }
      // Overlapping inline decorations can merge their DOM attributes. Read
      // every claim covering this text fragment from the canonical projection.
      const from = editor.view.posAtDOM(mark, 0)
      const to = editor.view.posAtDOM(mark, mark.childNodes.length)
      const claims = new Map<string, HoverClaim>()
      for (const expression of coworkExpressionTargets(editor)) {
        if (expression.from >= to || expression.to <= from) continue
        for (const id of expression.claimIds) claims.set(id, {
          id, proposition: expression.proposition || "Claim recorded at this passage",
          status: expression.stale ? "Passage needs review" : expression.isFact === true ? "Fact" :
            (expression.claimStatus ?? "unknown").split("_").join(" "),
          evidenceCount: expression.evidenceCount ?? 0,
        })
      }
      if (claims.size === 0) { clear(); return }
      cancelHide()
      current = mark
      const rect = mark.getBoundingClientRect()
      setHover({ claims: [...claims.values()], anchor: { left: rect.left, top: rect.top, bottom: rect.bottom },
        left: rect.left, top: rect.bottom + 6, width: Math.min(320, window.innerWidth - 16),
        maxHeight: window.innerHeight - 16, positioned: false })
    }
    const over = (event: PointerEvent) => show(event.target instanceof Element ? event.target : null)
    const leave = () => { cancelHide(); hideTimer.current = setTimeout(clear, 160) }
    const selection = () => {
      if (!editor.state.selection.empty || expandedSelectionTouches(root)) { clear(); return }
      if (!root.contains(document.activeElement)) return
      const node = document.getSelection()?.anchorNode
      show(node instanceof Element ? node : node?.parentElement ?? null)
    }
    const key = (event: KeyboardEvent) => { if (event.key === "Escape") clear() }
    const blur = (event: FocusEvent) => {
      if (event.relatedTarget instanceof Node && (root.contains(event.relatedTarget) || card.current?.contains(event.relatedTarget))) return
      clear()
    }
    const refresh = () => {
      if (current?.isConnected) show(current)
      else if (current !== null) clear()
    }
    const scroll = (event: Event) => {
      if (event.target instanceof Node && card.current?.contains(event.target)) return
      clear()
    }
    const observer = new MutationObserver(refresh)
    observer.observe(root, { childList: true, subtree: true, attributes: true,
      attributeFilter: ["data-wb-is-fact", "data-wb-stale", "data-claim-status", "data-wb-evidence-count"] })
    root.addEventListener("pointerover", over)
    root.addEventListener("pointerleave", leave)
    root.addEventListener("focusout", blur)
    document.addEventListener("selectionchange", selection)
    document.addEventListener("keydown", key)
    window.addEventListener("scroll", scroll, true)
    window.addEventListener("resize", refresh)
    const viewport = window.visualViewport
    viewport?.addEventListener("resize", refresh)
    viewport?.addEventListener("scroll", clear)
    editor.on("transaction", refresh)
    return () => {
      clear()
      observer.disconnect()
      root.removeEventListener("pointerover", over)
      root.removeEventListener("pointerleave", leave)
      root.removeEventListener("focusout", blur)
      document.removeEventListener("selectionchange", selection)
      document.removeEventListener("keydown", key)
      window.removeEventListener("scroll", scroll, true)
      window.removeEventListener("resize", refresh)
      viewport?.removeEventListener("resize", refresh)
      viewport?.removeEventListener("scroll", clear)
      editor.off("transaction", refresh)
    }
  }, [active, editor])
  useLayoutEffect(() => {
    if (hover === null || card.current === null) return
    const rect = card.current.getBoundingClientRect()
    const viewport = window.visualViewport
    const viewportTop = (viewport?.offsetTop ?? 0) + 8
    const viewportLeft = (viewport?.offsetLeft ?? 0) + 8
    const viewportBottom = viewportTop + (viewport?.height ?? window.innerHeight) - 16
    const width = Math.max(1, Math.min(320, (viewport?.width ?? window.innerWidth) - 16))
    const left = Math.max(viewportLeft, Math.min(hover.anchor.left,
      viewportLeft + (viewport?.width ?? window.innerWidth) - 16 - width))
    const desiredHeight = Math.max(rect.height, card.current.scrollHeight + 2)
    const aboveEnd = Math.min(hover.anchor.top - 6, viewportBottom)
    const belowStart = Math.max(hover.anchor.bottom + 6, viewportTop)
    const above = Math.max(0, aboveEnd - viewportTop)
    const below = Math.max(0, viewportBottom - belowStart)
    const placeBelow = below >= desiredHeight || (above < desiredHeight && below >= above)
    const maxHeight = placeBelow ? below : above
    const top = placeBelow ? belowStart : aboveEnd - Math.min(desiredHeight, maxHeight)
    if (!hover.positioned || left !== hover.left || top !== hover.top || width !== hover.width || maxHeight !== hover.maxHeight) {
      setHover({ ...hover, left, top, width, maxHeight, positioned: true })
    }
  }, [hover])
  return !active || hover === null ? null : createPortal(
    <aside className="wb-cowork-truth-hover" aria-label="Claim at this passage" ref={card}
      style={{ left: hover.left, top: hover.top, width: hover.width, maxHeight: hover.maxHeight,
        visibility: hover.positioned && hover.maxHeight > 26 ? undefined : "hidden" }} onPointerEnter={cancelHide}
      onPointerLeave={() => setHover(null)}>
      {hover.claims.map((claim) => <section key={claim.id}>
        <strong>{claim.proposition}</strong>
        <p>{claim.status}</p>
        <p>{claim.evidenceCount === 0 ? "No evidence recorded" : `${claim.evidenceCount} evidence ${claim.evidenceCount === 1 ? "receipt" : "receipts"}`}</p>
        <button type="button" onClick={() => { onOpenClaim(claim.id); setHover(null) }}>Open in Truth</button>
      </section>)}
    </aside>, document.body,
  )
}

export function TruthLegend({ editor, active }: { readonly editor: Editor | null; readonly active: boolean }) {
  const [counts, setCounts] = useState({ claims: 0, facts: 0, toJudge: 0, missing: 0 })
  useEffect(() => {
    if (editor === null) return
    const update = () => {
      const next = coworkTruthLegendCounts(editor)
      setCounts((prior) => JSON.stringify(prior) === JSON.stringify(next) ? prior : next)
    }
    update()
    editor.on("transaction", update)
    return () => { editor.off("transaction", update) }
  }, [editor])
  if (!active) return null
  return <div className="wb-cowork-truth-legend" aria-label="Truth mark legend">
    <span>{counts.claims} {counts.claims === 1 ? "claim" : "claims"} in view</span><span aria-hidden="true"> · </span>
    <span className="wb-cowork-truth-legend__fact">{counts.facts} {counts.facts === 1 ? "fact" : "facts"}</span><span aria-hidden="true"> · </span>
    <span className="wb-cowork-truth-legend__proposed">{counts.toJudge} to judge</span>
    <span className="wb-cowork-truth-legend__attention">! Needs review or challenged</span>
    <span className="wb-cowork-truth-legend__stale">Stale passage</span>
    {counts.missing ? <span>{counts.missing} passages could not be located. Open their claims in Truth.</span> : null}
  </div>
}
