/**
 * The selection-triggered "Give feedback" affordance in the live editor path
 * (PRD job 5, section 7 feedback walkthrough). Highlight a passage and a small
 * floating control appears near the selection. Activating it opens a compact
 * verbatim-text input. On submit the affordance POSTs the R9 feedback route with
 * the document id, the selection's quote anchor, and the verbatim text, then
 * hands the capture up so the Chat tab renders the span-linked message and the
 * rail switches to Chat (PRD: the human sees the feedback land).
 *
 * The affordance authors CONTENT, it is not a gesture (PRD section 6): the text
 * is saved verbatim as user-authored evidence, so a failed POST never discards
 * what the user typed. The whole geometry layer is defensive: coordsAtPos is
 * wrapped so a headless test environment degrades to a default corner rather
 * than throwing, and the anchor is frozen when the input opens so a later
 * selection change does not move the passage under the feedback.
 */

import { useCallback, useEffect, useId, useMemo, useState } from "react";
import type { Editor } from "@tiptap/core";

import type { FeedbackCapture } from "../chat";
import {
  DEFAULT_FEEDBACK_CONTEXT_CHARS,
  quoteAnchorFromRange,
  type RangeQuoteAnchor,
} from "./feedbackAnchor";
import {
  HttpCoworkFeedbackTransport,
  type CoworkFeedbackTransport,
} from "./feedbackClient";
import "./styles.css";

/** Shown on the disabled trigger when the document has no live scope. */
const LIVE_REQUIRED_TITLE =
  "Open this document in a live scope to give feedback.";

interface FloatPos {
  readonly left: number;
  readonly top: number;
}

interface Draft {
  readonly anchor: RangeQuoteAnchor;
  readonly pos: FloatPos | null;
}

export interface CoworkFeedbackAffordanceProps {
  /** The mounted live editor whose selection drives the affordance. */
  readonly editor: Editor;
  /** The cowork doc id the feedback is anchored in. */
  readonly documentId: string;
  /**
   * The scope store id. Absent or empty (demo / no live scope) renders the
   * trigger disabled with an explanatory title and never POSTs.
   */
  readonly storeId?: string;
  /**
   * Notified with the R9 capture so the surface annotates the Chat tab and
   * switches the rail to Chat. Called only on a successful capture.
   */
  readonly onCaptured: (capture: FeedbackCapture) => void;
  /** Injectable R9 transport, else the same-origin HTTP transport. */
  readonly transport?: CoworkFeedbackTransport;
  readonly contextChars?: number;
}

/**
 * Position the affordance near the selection end, relative to the editor host so
 * absolute placement lands inside it. Wrapped because coordsAtPos throws in a
 * headless DOM, where a null return falls back to the stylesheet's corner.
 */
const computeFloatPos = (editor: Editor, to: number): FloatPos | null => {
  try {
    const coords = editor.view.coordsAtPos(to);
    const host =
      (editor.view.dom.closest(".wb-cowork-editor") as HTMLElement | null) ??
      editor.view.dom;
    const rect = host.getBoundingClientRect();
    return { left: coords.left - rect.left, top: coords.bottom - rect.top };
  } catch {
    return null;
  }
};

export function CoworkFeedbackAffordance({
  editor,
  documentId,
  storeId,
  onCaptured,
  transport,
  contextChars = DEFAULT_FEEDBACK_CONTEXT_CHARS,
}: CoworkFeedbackAffordanceProps) {
  const resolvedTransport = useMemo(
    () => transport ?? new HttpCoworkFeedbackTransport(),
    [transport],
  );
  const fieldId = useId();

  const [selection, setSelection] = useState<{
    from: number;
    to: number;
  } | null>(null);
  const [triggerPos, setTriggerPos] = useState<FloatPos | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const isLive = typeof storeId === "string" && storeId.length > 0;

  // Track the live text selection so the trigger appears only over a real span.
  useEffect(() => {
    const sync = () => {
      const { from, to, empty } = editor.state.selection;
      if (empty || to <= from) {
        setSelection(null);
        setTriggerPos(null);
        return;
      }
      setSelection({ from, to });
      setTriggerPos(computeFloatPos(editor, to));
    };
    sync();
    editor.on("selectionUpdate", sync);
    return () => {
      editor.off("selectionUpdate", sync);
    };
  }, [editor]);

  const openDraft = useCallback(() => {
    if (selection === null) return;
    const anchor = quoteAnchorFromRange(
      editor.state.doc,
      selection.from,
      selection.to,
      contextChars,
    );
    if (anchor === null) return;
    setDraft({ anchor, pos: triggerPos });
    setText("");
    setError(null);
  }, [editor, selection, triggerPos, contextChars]);

  const cancelDraft = useCallback(() => {
    setDraft(null);
    setText("");
    setError(null);
    setSubmitting(false);
  }, []);

  const submit = useCallback(async () => {
    if (draft === null || submitting) return;
    if (text.trim().length === 0) {
      setError("Write a note before sending.");
      return;
    }
    if (!isLive || storeId === undefined) {
      setError(LIVE_REQUIRED_TITLE);
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const response = await resolvedTransport.submit({
        documentId,
        storeId,
        span: {
          exact: draft.anchor.exact,
          prefix: draft.anchor.prefix,
          suffix: draft.anchor.suffix,
          node_id_hint: null,
        },
        // Verbatim, exactly as typed, so the capture matches the message R9 posts.
        text,
      });
      if (!response.ok) {
        throw new Error("Feedback was not accepted. Try again.");
      }
      onCaptured({
        evidenceId: response.evidence_id,
        spanId: response.span_id,
        conversationId: response.conversation_id,
        text,
        anchor: {
          exact: draft.anchor.exact,
          prefix: draft.anchor.prefix,
          suffix: draft.anchor.suffix,
        },
      });
      setDraft(null);
      setText("");
      setError(null);
    } catch (caught) {
      // Never lose the user's words: keep the draft open with the typed text.
      setError(
        caught instanceof Error
          ? caught.message
          : "Feedback could not be sent.",
      );
    } finally {
      setSubmitting(false);
    }
  }, [
    draft,
    submitting,
    text,
    isLive,
    storeId,
    resolvedTransport,
    documentId,
    onCaptured,
  ]);

  // Nothing to show without a selection and without an open draft.
  if (selection === null && draft === null) return null;

  const pos = draft?.pos ?? triggerPos;
  const style = pos === null ? undefined : { left: pos.left, top: pos.top };

  return (
    <div className="wb-cowork-feedback" style={style}>
      {draft === null ? (
        <button
          type="button"
          className="wb-cowork-feedback__trigger"
          disabled={!isLive}
          title={isLive ? undefined : LIVE_REQUIRED_TITLE}
          // Keep the editor selection while the trigger takes the click, so the
          // anchor is still live when the draft opens (the bubble-menu idiom).
          onMouseDown={(event) => event.preventDefault()}
          onClick={openDraft}
        >
          Give feedback
        </button>
      ) : (
        <form
          className="wb-cowork-feedback__form"
          aria-label="Give feedback on the selected passage"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <label className="wb-cowork-feedback__label" htmlFor={fieldId}>
            Feedback on the selected passage
          </label>
          <blockquote className="wb-cowork-feedback__quote">
            {draft.anchor.exact}
          </blockquote>
          <textarea
            id={fieldId}
            className="wb-cowork-feedback__input"
            value={text}
            onChange={(event) => setText(event.target.value)}
            rows={3}
            autoFocus
            disabled={submitting}
            placeholder="What should change here, or what would you like to ask?"
          />
          {error !== null ? (
            <p className="wb-cowork-feedback__error" role="alert">
              {error}
            </p>
          ) : null}
          <div className="wb-cowork-feedback__actions">
            <button
              type="submit"
              className="wb-cowork-feedback__send"
              disabled={submitting}
            >
              {submitting ? "Sending..." : "Send feedback"}
            </button>
            <button
              type="button"
              className="wb-cowork-feedback__cancel"
              onClick={cancelDraft}
              disabled={submitting}
            >
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

export default CoworkFeedbackAffordance;
