import {
  Dialog,
  Heading,
  Modal,
  ModalOverlay,
} from "react-aria-components";

import { Button, InlineAlert } from "../../../ui";
import { CoworkProvenanceForm } from "./CoworkProvenanceForm";
import {
  coworkProvenanceDeterminationIssue,
  type CoworkProvenanceActorIdentity,
  type CoworkProvenanceDetermination,
} from "./contracts";

export interface CoworkProvenanceDeterminationDialogProps {
  readonly value: CoworkProvenanceDetermination;
  readonly currentUserIdentity: CoworkProvenanceActorIdentity;
  readonly title?: string;
  readonly description?: string;
  readonly passageExcerpt?: string;
  readonly passageLabel?: string;
  readonly confirmLabel?: string;
  readonly cancelLabel?: string;
  readonly busy?: boolean;
  /** Lock a frozen ambiguous request while leaving its retry action available. */
  readonly formDisabled?: boolean;
  readonly error?: string | null;
  onChange(value: CoworkProvenanceDetermination): void;
  onConfirm(value: CoworkProvenanceDetermination): void | Promise<void>;
  onClose(): void;
}

/**
 * Modal wrapper for provenance determinations that occur after content already
 * exists, such as a substantial paste. Import flows reuse the form directly.
 */
export function CoworkProvenanceDeterminationDialog({
  value,
  currentUserIdentity,
  title = "Where did this text come from?",
  description = "Record its authorship and, for AI-written text, whether a person reviewed it.",
  passageExcerpt,
  passageLabel = "Pasted passage",
  confirmLabel = "Save",
  cancelLabel = "Decide later",
  busy = false,
  formDisabled = false,
  error = null,
  onChange,
  onConfirm,
  onClose,
}: CoworkProvenanceDeterminationDialogProps) {
  const issue = coworkProvenanceDeterminationIssue(value);
  const close = (): void => {
    if (!busy) onClose();
  };

  return (
    <ModalOverlay
      isOpen
      isDismissable={!busy}
      onOpenChange={(open) => {
        if (!open) close();
      }}
      className="wb-cowork-dialog-overlay"
    >
      <Modal className="wb-cowork-dialog">
        <Dialog
          aria-labelledby="wb-cowork-provenance-dialog-title"
          aria-busy={busy}
          className="wb-cowork-dialog__body"
        >
          <Heading id="wb-cowork-provenance-dialog-title" slot="title">
            {title}
          </Heading>
          <p className="wb-cowork-provenance-dialog__description">
            {description}
          </p>
          {passageExcerpt !== undefined && passageExcerpt.length > 0 ? (
            <blockquote
              className="wb-cowork-provenance-dialog__excerpt"
              aria-label={passageLabel}
            >
              {passageExcerpt}
            </blockquote>
          ) : null}
          <span
            className="wb-visually-hidden"
            role="status"
            aria-live="polite"
          >
            {busy ? "Saving provenance…" : ""}
          </span>

          {error !== null ? (
            <InlineAlert tone="danger" role="alert">
              {error}
            </InlineAlert>
          ) : null}

          <CoworkProvenanceForm
            value={value}
            currentUserIdentity={currentUserIdentity}
            disabled={busy || formDisabled}
            onChange={onChange}
          />

          <div className="wb-cowork-dialog__actions">
            <Button onClick={close} disabled={busy}>
              {cancelLabel}
            </Button>
            <Button
              variant="primary"
              onClick={() => void onConfirm(value)}
              disabled={busy || issue !== null}
              title={issue ?? undefined}
            >
              {busy ? "Saving…" : confirmLabel}
            </Button>
          </div>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
