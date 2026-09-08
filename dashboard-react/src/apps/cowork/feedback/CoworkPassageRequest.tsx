import { useId, useMemo, useRef, useState } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";
import type { RangeQuoteAnchor } from "./feedbackAnchor";
import { HttpCoworkFeedbackTransport, type CoworkFeedbackTransport } from "./feedbackClient";
import type { FeedbackCapture } from "../chat";
import "./styles.css";
import "../menus/documentActions.css";

export interface CoworkPassageRequestProps {
  readonly anchor: RangeQuoteAnchor;
  readonly documentId: string;
  readonly storeId: string;
  readonly transport?: CoworkFeedbackTransport;
  readonly blockedReason?: string | null;
  readonly onCaptured: (capture: FeedbackCapture) => void;
  readonly onClose: () => void;
}

/** The note and its frozen passage remain intact when sending fails. */
export function CoworkPassageRequest({ anchor, documentId, storeId, transport, blockedReason = null, onCaptured, onClose }: CoworkPassageRequestProps) {
  const fieldId = useId();
  const titleId = useId();
  const client = useMemo(() => transport ?? new HttpCoworkFeedbackTransport(), [transport]);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [captured, setCaptured] = useState<FeedbackCapture | null>(null);
  const blockedReasonRef = useRef(blockedReason);
  blockedReasonRef.current = blockedReason;
  const sendBlockedReason = captured === null ? blockedReason : null;
  const close = () => { if (!busyRef.current) onClose(); };
  return <ModalOverlay isOpen isDismissable={!busy} isKeyboardDismissDisabled={busy}
    onOpenChange={(open) => { if (!open) close(); }} className="wb-cowork-dialog-overlay">
    <Modal className="wb-cowork-passage-request">
      <Dialog aria-labelledby={titleId} aria-busy={busy}>
        <form className="wb-cowork-feedback__form" onSubmit={async (event) => {
          event.preventDefault();
          if (busyRef.current) return;
          if (captured === null && blockedReasonRef.current !== null) {
            return;
          }
          if (!text.trim()) { setError("Write a note before sending."); return; }
          busyRef.current = true;
          setBusy(true);
          setError(null);
          let receipt = captured;
          try {
            if (receipt === null) {
              const response = await client.submit({ documentId, storeId, span: { ...anchor, node_id_hint: null }, text });
              if (!response.ok) throw new Error("The change request was not accepted. Try again.");
              receipt = { documentId, storeId, evidenceId: response.evidence_id, spanId: response.span_id,
                messageId: response.message_id, conversationId: response.conversation_id, agent: response.agent,
                execution: response.execution, text, anchor };
              setCaptured(receipt);
            }
          } catch (cause) {
            setError(cause instanceof Error ? cause.message : "The change request could not be sent.");
          }
          if (receipt !== null) {
            try {
              onCaptured(receipt);
              onClose();
            } catch {
              setError("Your request was sent, but Chat could not open. Try again.");
            }
          }
          busyRef.current = false;
          setBusy(false);
        }}>
          <Heading id={titleId} slot="title">Request a change here</Heading>
          <blockquote className="wb-cowork-feedback__quote" aria-label="Selected passage">{anchor.exact}</blockquote>
          <label htmlFor={fieldId} className="wb-cowork-feedback__label">Change request</label>
          <textarea id={fieldId} autoFocus className="wb-cowork-feedback__input" value={text}
            onChange={(event) => setText(event.target.value)} rows={4} disabled={busy || captured !== null}
            placeholder="Describe the change you want in this passage." />
          {sendBlockedReason === null && error === null ? null : <p className="wb-cowork-feedback__error" role="alert">{sendBlockedReason ?? error}</p>}
          <div className="wb-cowork-feedback__actions">
            <button className="wb-cowork-feedback__send" type="submit" disabled={busy || sendBlockedReason !== null}>
              {busy ? "Sending…" : captured !== null ? "Open in Chat" : "Send change request"}
            </button>
            <button className="wb-cowork-feedback__cancel" type="button" disabled={busy} onClick={close}>Cancel</button>
          </div>
        </form>
      </Dialog>
    </Modal>
  </ModalOverlay>;
}
