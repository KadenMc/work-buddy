import { useEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import type { Editor } from "@tiptap/core"
import { Menu, MenuItem, Popover, Text } from "react-aria-components"
import { shortcutMatchesEvent } from "../../../settings/keybindings"
import { DEFAULT_COWORK_SHORTCUT_BINDINGS } from "../keyboard"
import type { CoworkEditorLens } from "../editor/ledgerDecorations"
import type { FeedbackCapture } from "../chat"
import type { CoworkFeedbackTransport } from "../feedback/feedbackClient"
import { CoworkPassageRequest } from "../feedback/CoworkPassageRequest"
import { CoworkProvenanceDeterminationDialog } from "../provenance/CoworkProvenanceDeterminationDialog"
import { classifyCoworkProvenanceSelection } from "../provenance/CoworkProvenanceSelectionAffordance"
import { defaultCoworkProvenanceDetermination, type CoworkProvenanceActorIdentity, type CoworkProvenanceDetermination } from "../provenance/contracts"
import type { ProvenanceLoad, ProvenanceProvider, ProvenanceSelectionAction } from "../provenance/view/contracts"
import { coworkTruthMenuItems, resolveCoworkPassageTarget, selectCoworkPassageTarget,
  type CoworkDocumentActionItem, type CoworkDocumentActions, type CoworkPassageTarget } from "./documentActions"
import "./documentActions.css"

export interface CoworkDocumentContextMenuProps {
  readonly editor: Editor
  readonly activeLens: CoworkEditorLens
  readonly actions?: CoworkDocumentActions
  readonly active?: boolean
  readonly readOnly?: boolean
  readonly documentId?: string
  readonly storeId?: string
  readonly feedbackTransport?: CoworkFeedbackTransport
  readonly onFeedbackCaptured?: (capture: FeedbackCapture) => void
  readonly provenance?: {
    readonly provider: ProvenanceProvider
    readonly currentUserIdentity?: CoworkProvenanceActorIdentity
    readonly onRecord: (anchor: ProvenanceSelectionAction["anchor"], value: CoworkProvenanceDetermination) => Promise<void>
    readonly onAction: (action: ProvenanceSelectionAction & { readonly intent: "review" | "view" | "inspect" }) => void
  }
}

interface MenuState { readonly target: CoworkPassageTarget; readonly left: number; readonly top: number }
let provenanceRequest = 0
const fallbackActions: CoworkDocumentActions = {}

export function CoworkDocumentContextMenu(props: CoworkDocumentContextMenuProps) {
  const { editor, activeLens, actions = fallbackActions, readOnly = false, provenance } = props
  const active = props.active ?? actions.active ?? true
  const latest = useRef(props)
  latest.current = props
  const triggerRef = useRef<HTMLSpanElement>(null)
  const restoreEditorFocus = useRef(false)
  const [menu, setMenu] = useState<MenuState | null>(null)
  const [handle, setHandle] = useState<{ left: number; top: number } | null>(null)
  const [request, setRequest] = useState<CoworkPassageTarget | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [provenanceLoad, setProvenanceLoad] = useState<ProvenanceLoad | null>(null)
  const [locallyDirty, setLocallyDirty] = useState(false)
  const [recording, setRecording] = useState<{ target: CoworkPassageTarget; value: CoworkProvenanceDetermination } | null>(null)
  const [recordBusy, setRecordBusy] = useState(false)
  const [recordError, setRecordError] = useState<string | null>(null)
  const [, refreshActions] = useState(0)
  useEffect(() => actions.truthStore?.subscribeActions(() => refreshActions((value) => value + 1)), [actions.truthStore])
  useEffect(() => {
    if (provenance === undefined || activeLens !== "provenance") return
    let mounted = true
    const load = () => void provenance.provider.load().then((value) => {
      if (mounted) { setProvenanceLoad(value); setLocallyDirty(false) }
    }, () => { if (mounted) setProvenanceLoad(null) })
    load()
    const unsubscribe = provenance.provider.subscribe(load)
    return () => { mounted = false; unsubscribe() }
  }, [activeLens, provenance?.provider])
  useEffect(() => {
    const dirty = () => { setLocallyDirty(true); setMenu(null) }
    editor.on("update", dirty)
    return () => { editor.off("update", dirty) }
  }, [editor])
  useEffect(() => { setMenu(null) }, [activeLens, active, editor])
  useEffect(() => {
    if (menu !== null || !restoreEditorFocus.current) return
    restoreEditorFocus.current = false
    const frame = requestAnimationFrame(() => {
      if (!editor.isDestroyed && active) editor.view.focus()
    })
    return () => cancelAnimationFrame(frame)
  }, [menu, editor, active])

  const focusEditor = () => { if (!editor.isDestroyed) editor.view.focus() }
  const open = (position?: number, coordinates?: { left: number; top: number }) => {
    if (!(latest.current.active ?? latest.current.actions?.active ?? true)) return
    const target = resolveCoworkPassageTarget(editor, position)
    if (target === null) return
    let point = coordinates
    if (point === undefined) {
      try { const rect = editor.view.coordsAtPos(editor.state.selection.from); point = { left: rect.left, top: rect.bottom } }
      catch { const rect = editor.view.dom.getBoundingClientRect(); point = { left: rect.left, top: rect.top } }
    }
    setError(null)
    focusEditor()
    const viewport = window.visualViewport
    const left = viewport?.offsetLeft ?? 0
    const top = viewport?.offsetTop ?? 0
    const right = left + (viewport?.width ?? window.innerWidth)
    const bottom = top + (viewport?.height ?? window.innerHeight)
    setMenu({ target,
      left: Math.max(left + 12, Math.min(point.left, right - 12)),
      top: Math.max(top + 12, Math.min(point.top, bottom - 12)),
    })
  }

  useEffect(() => {
    if (!active) return
    const root = editor.view.dom
    const context = (event: MouseEvent) => {
      const position = editor.view.posAtCoords({ left: event.clientX, top: event.clientY })?.pos
      if (position === undefined) return
      if (resolveCoworkPassageTarget(editor, position) === null) return
      event.preventDefault()
      open(position, { left: event.clientX, top: event.clientY })
    }
    const key = (event: KeyboardEvent) => {
      if (event.isComposing || event.defaultPrevented || event.repeat) return
      if (!(latest.current.active ?? latest.current.actions?.active ?? true)) return
      if (!event.altKey && !event.ctrlKey && !event.metaKey &&
        (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10"))) {
        if (resolveCoworkPassageTarget(editor) === null) return
        event.preventDefault()
        event.stopPropagation()
        open()
      } else if (shortcutMatchesEvent(latest.current.actions?.shortcutBindings?.openClaim ?? DEFAULT_COWORK_SHORTCUT_BINDINGS.openClaim, event)) {
        const target = resolveCoworkPassageTarget(editor)
        if (target?.claims.length && latest.current.actions?.onOpenClaim !== undefined) {
          event.preventDefault()
          event.stopPropagation()
          if (target.claims.length === 1) latest.current.actions?.onOpenClaim?.(target.claims[0]!.id)
          else open()
        }
      }
    }
    const click = (event: MouseEvent) => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey ||
        latest.current.activeLens !== "truth" || !editor.state.selection.empty) return
      const mark = event.target instanceof Element ? event.target.closest("[data-wb-anchor-kind='expression']") : null
      if (mark === null) return
      const position = editor.view.posAtCoords({ left: event.clientX, top: event.clientY })?.pos
      if (position === undefined) return
      const target = resolveCoworkPassageTarget(editor, position)
      if (target?.claims.length === 1) latest.current.actions?.onOpenClaim?.(target.claims[0]!.id)
      else if (target?.claims.length) open(position, { left: event.clientX, top: event.clientY })
    }
    const sync = () => {
      if (editor.state.selection.empty) { setHandle(null); return }
      try {
        const rect = editor.view.coordsAtPos(editor.state.selection.to)
        setHandle({ left: Math.max(8, Math.min(rect.left, window.innerWidth - 145)), top: Math.max(8, Math.min(rect.bottom + 6, window.innerHeight - 42)) })
      } catch { setHandle(null) }
    }
    sync()
    root.addEventListener("contextmenu", context)
    // Run before ProseMirror's Enter capture and Tiptap's editing keymaps.
    root.addEventListener("keydown", key, true)
    root.addEventListener("click", click)
    editor.on("selectionUpdate", sync)
    window.addEventListener("scroll", sync, true)
    window.addEventListener("resize", sync)
    return () => {
      root.removeEventListener("contextmenu", context)
      root.removeEventListener("keydown", key, true)
      root.removeEventListener("click", click)
      editor.off("selectionUpdate", sync)
      window.removeEventListener("scroll", sync, true)
      window.removeEventListener("resize", sync)
    }
  }, [active, editor])

  const items: CoworkDocumentActionItem[] = []
  if (menu !== null) {
    const target = menu.target
    const prepare = () => selectCoworkPassageTarget(editor, target)
    const description = target.anchor.exact.length > 110 ? `${target.anchor.exact.slice(0, 107)}…` : target.anchor.exact
    items.push({ id: "ask", label: "Ask about this part…", description,
      blockedReason: actions.onAsk === undefined ? "Chat is unavailable for this document." : null,
      run: async () => { prepare(); await actions.onAsk?.() } })
    items.push({ id: "request", label: "Request a change here…", description,
      blockedReason: readOnly ? "This document is read-only." : !props.onFeedbackCaptured || !props.storeId || !props.documentId ? "Change requests are unavailable for this document." : null,
      run: () => { if (!editor.state.doc.eq(target.doc)) throw new Error("The passage changed. Open its actions again."); setRequest(target) } })
    items.push({ id: "working", label: "Set as working target", description,
      blockedReason: actions.onSetWorkingTarget === undefined ? "Working targets are unavailable for this document." : null,
      run: () => { prepare(); actions.onSetWorkingTarget?.() } })
    items.push(...coworkTruthMenuItems(activeLens, target, actions, prepare, { readOnly }))
    if (activeLens === "provenance") {
      const classification = provenanceLoad?.state === "ready" ? classifyCoworkProvenanceSelection({
        data: provenanceLoad.data, doc: editor.state.doc, from: target.from, to: target.to,
        readOnly, currentUserIdentity: provenance?.currentUserIdentity, locallyDirty,
      }) : null
      for (const intent of ["record", "review", "view", "inspect"] as const) items.push({
        id: `provenance:${intent}`,
        label: { record: "Record provenance", review: "Mark as reviewed", view: "View provenance", inspect: "Inspect provenance" }[intent],
        description,
        blockedReason: readOnly && (intent === "record" || intent === "review") ? "This document is read-only." :
          provenance === undefined || classification === null ? "Provenance is preparing." :
          intent !== classification.intent ? "This action does not apply to the selected provenance coverage." : null,
        run: () => {
          if (classification === null || provenance === undefined) return
          if (!editor.state.doc.eq(target.doc)) throw new Error("The passage changed. Open its actions again.")
          if (intent === "record") {
            if (provenance.currentUserIdentity === undefined) return
            setRecordError(null)
            setRecording({ target, value: defaultCoworkProvenanceDetermination(provenance.currentUserIdentity) })
          } else {
            const text = editor.state.doc.textBetween(0, editor.state.doc.content.size, "\n")
            provenance.onAction({ requestId: ++provenanceRequest, intent, anchor: target.anchor,
              from: target.from, to: target.to, targetIds: classification.targetIds,
              coversWholeDocument: target.anchor.exact.trim() === text.trim(),
              ...(intent === "review" && provenance.currentUserIdentity !== undefined ? { reviewer: {
                ref: provenance.currentUserIdentity.ref, identityStatus: provenance.currentUserIdentity.identity_status,
              } } : {}),
            })
          }
        },
      })
    }
  }
  const run = (id: string) => {
    const item = items.find((candidate) => candidate.id === id)
    if (item === undefined || item.blockedReason !== null) return
    focusEditor()
    setMenu(null)
    void Promise.resolve().then(() => {
      if (!(latest.current.active ?? latest.current.actions?.active ?? true)) return
      const mutates = ["request", "analyze", "propose", "connect", "provenance:record", "provenance:review"].includes(item.id)
        || item.id.startsWith("confirm:") || item.id.startsWith("reject:")
      if (latest.current.readOnly && mutates) throw new Error("This document is read-only.")
      if (menu === null || !editor.state.doc.eq(menu.target.doc)) throw new Error("The passage changed. Open its actions again.")
      return item.run()
    }).catch((cause) => setError(cause instanceof Error ? cause.message : "This action could not be completed."))
  }
  return <>
    {active && handle !== null && menu === null && request === null && recording === null ? createPortal(
      <button type="button" className="wb-cowork-passage-handle" style={handle} aria-haspopup="menu"
        onMouseDown={(event) => event.preventDefault()} onClick={(event) => {
          const rect = event.currentTarget.getBoundingClientRect()
          open(undefined, { left: rect.left, top: rect.bottom })
        }}>Passage actions</button>, document.body,
    ) : null}
    {menu === null ? null : createPortal(<span ref={triggerRef} aria-hidden="true" className="wb-cowork-menu-anchor"
      style={{ left: menu.left, top: menu.top }} />, document.body)}
    <Popover isOpen={menu !== null} triggerRef={triggerRef} placement="bottom start" maxHeight={640} containerPadding={12}
      className="wb-cowork-document-menu"
      onOpenChange={(isOpen) => { if (!isOpen) { restoreEditorFocus.current = true; setMenu(null) } }}>
      <p className="wb-cowork-document-menu__heading">What’s here</p>
      {menu === null ? null : <p className="wb-cowork-document-menu__passage">{menu.target.anchor.exact}</p>}
      <Menu aria-label="Actions for this passage" autoFocus="first" onAction={(key) => run(String(key))}
        disabledKeys={items.filter((item) => item.blockedReason !== null).map((item) => item.id)}>
        {items.map((item) => <MenuItem key={item.id} id={item.id} textValue={item.label}>
          <Text slot="label">{item.label}</Text>
          <Text slot="description">{item.description}{item.blockedReason === null ? null : <span className="wb-cowork-document-menu__reason">{item.blockedReason}</span>}</Text>
        </MenuItem>)}
      </Menu>
    </Popover>
    {error === null ? null : <p className="wb-cowork-passage-error" role="alert">{error}<button type="button" onClick={() => setError(null)}>Dismiss</button></p>}
    {request === null || !props.documentId || !props.storeId ? null : <CoworkPassageRequest
      anchor={request.anchor} documentId={props.documentId} storeId={props.storeId} transport={props.feedbackTransport}
      blockedReason={!active ? "Return to this document before sending." : readOnly ? "This document is read-only." :
        props.onFeedbackCaptured === undefined ? "Change requests are unavailable for this document." : null}
      onCaptured={(capture) => {
        const onCaptured = latest.current.onFeedbackCaptured
        if (onCaptured === undefined) throw new Error("Chat is unavailable for this document.")
        onCaptured(capture)
      }} onClose={() => { setRequest(null); if (active) focusEditor() }} />}
    {recording === null || provenance?.currentUserIdentity === undefined ? null : <CoworkProvenanceDeterminationDialog
      title="Record provenance" description="Record who wrote this passage and whether it was reviewed. Its earlier source remains untracked."
      value={recording.value} currentUserIdentity={provenance.currentUserIdentity} passageExcerpt={recording.target.anchor.exact}
      passageLabel="Selected passage" confirmLabel="Record provenance" cancelLabel="Cancel" busy={recordBusy} error={recordError}
      onChange={(value) => setRecording((current) => current === null ? null : { ...current, value })}
      onClose={() => { if (!recordBusy) { setRecording(null); focusEditor() } }}
      onConfirm={async (value) => {
        if (recordBusy) return
        setRecordBusy(true)
        setRecordError(null)
        try {
          if (!editor.state.doc.eq(recording.target.doc)) throw new Error("The passage changed. Open its actions again.")
          await provenance.onRecord(recording.target.anchor, value)
          setRecording(null)
          focusEditor()
        }
        catch (cause) { setRecordError(cause instanceof Error ? cause.message : "Provenance could not be recorded.") }
        finally { setRecordBusy(false) }
      }} />}
  </>
}
