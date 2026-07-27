import { useState } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { Button, InlineAlert } from "../../../ui";
import { asCoworkApiError, coworkErrorMessage } from "../providers/errors";

interface CoworkLocalDiscardDialogProps {
  readonly title: string;
  readonly onClose: () => void;
  readonly onDiscard: () => Promise<void>;
}

/** Confirms permanent removal of a document that only exists in device storage. */
export function CoworkLocalDiscardDialog({
  title,
  onClose,
  onDiscard,
}: CoworkLocalDiscardDialogProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const discard = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await onDiscard();
    } catch (discardError) {
      setError(
        coworkErrorMessage(
          asCoworkApiError(discardError),
          "Co-work couldn’t discard this document.",
        ),
      );
      setBusy(false);
    }
  };

  return (
    <ModalOverlay
      isOpen
      isDismissable={!busy}
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
      className="wb-cowork-dialog-overlay"
    >
      <Modal className="wb-cowork-dialog">
        <Dialog
          aria-labelledby="cowork-local-discard-title"
          className="wb-cowork-dialog__body"
        >
          <Heading id="cowork-local-discard-title" slot="title">
            Discard this document?
          </Heading>
          <p>
            <strong>{title}</strong> is saved only on this device. Discarding it
            permanently removes that copy.
          </p>
          {error !== null ? (
            <InlineAlert tone="danger" role="alert">
              {error}
            </InlineAlert>
          ) : null}
          <div className="wb-cowork-dialog__actions">
            <Button onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button variant="danger" onClick={() => void discard()} disabled={busy}>
              {busy ? "Discarding…" : "Discard document"}
            </Button>
          </div>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
