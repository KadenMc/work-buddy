import { Editor } from "@tiptap/core"
import StarterKit from "@tiptap/starter-kit"
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { CoworkLedgerDecorations, projectCoworkLedgerDecorations, setCoworkEditorLens } from "../../editor/ledgerDecorations"
import { TruthHoverCard, TruthLegend } from "./TruthHoverCard"

let editor: Editor
let host: HTMLElement
const mount = () => {
  host = document.createElement("div")
  document.body.append(host)
  editor = new Editor({ element: host, content: "<p>A measured passage.</p>", extensions: [StarterKit, CoworkLedgerDecorations] })
  projectCoworkLedgerDecorations(editor, {
    edits: [], flags: [], provenance: [],
    expressions: [
      { expressionId: "one", spanId: "span-one", quote: "measured passage", claimRef: "first", claimStatus: "confirmed", isFact: true, proposition: "Measurements agree.", evidenceCount: 2 },
      { expressionId: "two", spanId: "span-two", quote: "measured passage", claimRef: "second", claimStatus: "proposed", isFact: false, proposition: "Measurements are complete.", evidenceCount: 0 },
    ],
    claims: [{ claimId: "first", expressionId: "one", spanId: "span-one", quote: "measured passage" },
      { claimId: "second", expressionId: "two", spanId: "span-two", quote: "measured passage" }],
  })
  setCoworkEditorLens(editor, "truth")
  return host.querySelector<HTMLElement>("[data-wb-anchor-kind='expression']")!
}
afterEach(() => {
  cleanup(); editor?.destroy(); host?.remove(); document.getSelection()?.removeAllRanges()
  vi.restoreAllMocks(); vi.unstubAllGlobals()
})

const mockGeometry = (mark: HTMLElement, anchor: DOMRect, contentHeight: number) => {
  const getRect = HTMLElement.prototype.getBoundingClientRect
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this === mark) return anchor
    if (!this.classList.contains("wb-cowork-truth-hover")) return getRect.call(this)
    const height = Math.min(contentHeight, Number.parseFloat(this.style.maxHeight) || contentHeight)
    return new DOMRect(Number.parseFloat(this.style.left), Number.parseFloat(this.style.top), Number.parseFloat(this.style.width), height)
  })
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("wb-cowork-truth-hover") ? contentHeight - 2 : 0
  })
}

describe("Truth passage explanation", () => {
  it("explains every overlapping claim and opens only the chosen one", () => {
    const mark = mount()
    const open = vi.fn()
    render(<TruthHoverCard editor={editor} active onOpenClaim={open} />)
    fireEvent.pointerOver(mark)
    expect(screen.getByText("Measurements agree.")).toBeVisible()
    expect(screen.getByText("Measurements are complete.")).toBeVisible()
    expect(screen.getByText("2 evidence receipts")).toBeVisible()
    expect(screen.getByText("No evidence recorded")).toBeVisible()
    expect(open).not.toHaveBeenCalled()
    fireEvent.click(screen.getAllByRole("button", { name: "Open in Truth" })[1])
    expect(open).toHaveBeenCalledWith("second")
    expect(screen.queryByRole("complementary")).toBeNull()
  })

  it("places a low passage's card above its mark without covering the click point or moving focus", () => {
    vi.stubGlobal("innerWidth", 1280)
    vi.stubGlobal("innerHeight", 720)
    const mark = mount()
    const anchor = new DOMRect(81, 580, 318, 23)
    mockGeometry(mark, anchor, 178)
    const open = vi.fn()
    render(<TruthHoverCard editor={editor} active onOpenClaim={open} />)
    const focused = document.activeElement
    const before = editor.getJSON()
    const selection = editor.state.selection.toJSON()

    fireEvent.pointerOver(mark, { clientX: 240, clientY: 592 })

    const card = screen.getByRole("complementary", { name: "Claim at this passage" })
    const rect = card.getBoundingClientRect()
    expect(rect.bottom).toBeLessThan(anchor.top)
    expect(rect.top).toBeGreaterThanOrEqual(8)
    expect(rect.top <= 592 && rect.bottom >= 592).toBe(false)
    expect(screen.getAllByRole("button", { name: "Open in Truth" })).toHaveLength(2)
    expect(document.activeElement).toBe(focused)
    expect(editor.getJSON()).toEqual(before)
    expect(editor.state.selection.toJSON()).toEqual(selection)
    expect(open).not.toHaveBeenCalled()
  })

  it("fits a tall card beside its mark in a short visual viewport, scrolls internally, and repositions after resize", () => {
    const viewport = Object.assign(new EventTarget(), { width: 390, height: 260, offsetLeft: 5, offsetTop: 20 })
    vi.stubGlobal("visualViewport", viewport)
    const mark = mount()
    const anchor = new DOMRect(350, 170, 25, 20)
    mockGeometry(mark, anchor, 360)
    render(<TruthHoverCard editor={editor} active onOpenClaim={vi.fn()} />)

    fireEvent.pointerOver(mark)

    const card = screen.getByRole("complementary", { name: "Claim at this passage" })
    let rect = card.getBoundingClientRect()
    expect(rect.top).toBeGreaterThanOrEqual(viewport.offsetTop + 8)
    expect(rect.bottom).toBeLessThan(anchor.top)
    expect(rect.right).toBeLessThanOrEqual(viewport.offsetLeft + viewport.width - 8)
    expect(Number.parseFloat(card.style.maxHeight)).toBeLessThan(360)
    fireEvent.scroll(card)
    expect(screen.getByRole("complementary")).toBe(card)
    expect(screen.getByText("Measurements agree.")).toBeVisible()
    expect(screen.getByText("Measurements are complete.")).toBeVisible()

    act(() => { viewport.height = 700; viewport.dispatchEvent(new Event("resize")) })

    rect = card.getBoundingClientRect()
    expect(rect.top).toBeGreaterThan(anchor.bottom)
    expect(rect.bottom).toBeLessThanOrEqual(viewport.offsetTop + viewport.height - 8)
    expect(Number.parseFloat(card.style.maxHeight)).toBeGreaterThanOrEqual(360)
    fireEvent.scroll(editor.view.dom)
    expect(screen.queryByRole("complementary")).toBeNull()
  })

  it("suppresses the explanation during browser text selection and outside the Truth lens", () => {
    const mark = mount()
    const view = render(<TruthHoverCard editor={editor} active onOpenClaim={vi.fn()} />)
    fireEvent.pointerOver(mark)
    expect(screen.getByRole("complementary")).toBeVisible()
    const range = document.createRange()
    range.selectNodeContents(mark)
    document.getSelection()?.addRange(range)
    fireEvent(document, new Event("selectionchange"))
    expect(screen.queryByRole("complementary")).toBeNull()
    document.getSelection()?.removeAllRanges()
    fireEvent.pointerOver(mark)
    view.rerender(<TruthHoverCard editor={editor} active={false} onOpenClaim={vi.fn()} />)
    expect(screen.queryByRole("complementary")).toBeNull()
  })

  it("counts server facts and removes terminal claims after a projection refresh", () => {
    mount()
    render(<TruthLegend editor={editor} active />)
    expect(screen.getByText("2 claims in view")).toBeVisible()
    expect(screen.getByText("1 fact")).toBeVisible()
    expect(screen.getByText("1 to judge")).toBeVisible()
    act(() => projectCoworkLedgerDecorations(editor, { edits: [], flags: [], provenance: [], claims: [], expressions: [] }))
    expect(screen.getByText("0 claims in view")).toBeVisible()
  })
})
