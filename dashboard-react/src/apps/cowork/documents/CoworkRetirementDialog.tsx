import { useEffect, useRef, useState } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { Button, InlineAlert, Spinner } from "../../../ui";
import type { CoworkDocumentSummary } from "../contracts";
import {
  CoworkHttpClient,
  type CoworkRetirementPrepared,
  type CoworkRetirementReceipt,
} from "../providers/CoworkHttpClient";
import { asCoworkApiError, coworkErrorMessage } from "../providers/errors";

const makeIdempotencyKey = (): string =>
  globalThis.crypto?.randomUUID?.() ??
  `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;

export interface CoworkRetirementDialogProps {
  readonly storeId: string;
  readonly document: CoworkDocumentSummary;
  readonly client: CoworkHttpClient;
  readonly onClose: () => void;
  readonly onRetired: (receipt: CoworkRetirementReceipt) => Promise<void> | void;
}

/** Server-prepared, non-destructive confirmation for removing a document from Co-work. */
export function CoworkRetirementDialog({
  storeId,
  document,
  client,
  onClose,
  onRetired,
}: CoworkRetirementDialogProps) {
  const [prepared, setPrepared] = useState<CoworkRetirementPrepared | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const keyRef = useRef(makeIdempotencyKey());

  const prepare = (): void => {
    setBusy(true);
    setError(null);
    void client
      .prepareRetirement(storeId, document.documentId, keyRef.current)
      .then(setPrepared)
      .catch((prepareError) =>
        setError(
          coworkErrorMessage(
            asCoworkApiError(prepareError),
            "Co-work couldn’t check that document for safe removal.",
          ),
        ),
      )
      .finally(() => setBusy(false));
  };

  useEffect(prepare, [client, document.documentId, storeId]);

  const retire = async (): Promise<void> => {
    if (prepared === null) return;
    setBusy(true);
    setError(null);
    try {
      const receipt = await client.commitRetirement(
        storeId,
        document.documentId,
        prepared.intentId,
      );
      await onRetired(receipt);
    } catch (retireError) {
      setError(
        coworkErrorMessage(
          asCoworkApiError(retireError),
          "Co-work couldn’t remove that document.",
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
        <Dialog aria-labelledby="cowork-retire-title" className="wb-cowork-dialog__body">
          <Heading id="cowork-retire-title" slot="title">Remove from Co-work?</Heading>
          <p>
            This stops managing <strong>{document.title}</strong> as a Co-work document.
            It is not a file deletion.
          </p>
          {prepared !== null ? (
            <InlineAlert tone="warning">
              <strong>Confirm the exact consequence</strong>
              <span>{prepared.consequence}</span>
            </InlineAlert>
          ) : null}
          {error !== null ? <InlineAlert tone="danger" role="alert">{error}</InlineAlert> : null}
          {busy ? <p role="status"><Spinner /> {prepared === null ? "Checking safe removal…" : "Removing from Co-work…"}</p> : null}
          <div className="wb-cowork-dialog__actions">
            <Button onClick={onClose} disabled={busy}>Cancel</Button>
            {prepared === null && error !== null ? (
              <Button onClick={prepare} disabled={busy}>Retry check</Button>
            ) : (
              <Button variant="danger" onClick={() => void retire()} disabled={busy || prepared === null}>
                Remove from Co-work
              </Button>
            )}
          </div>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
