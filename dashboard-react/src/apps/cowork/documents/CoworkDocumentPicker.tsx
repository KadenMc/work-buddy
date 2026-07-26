import { useMemo, useRef, useState } from "react";
import {
  Dialog,
  Heading,
  Input,
  Label,
  ListBox,
  ListBoxItem,
  Modal,
  ModalOverlay,
  TextField,
  type Key,
} from "react-aria-components";

import { Button, InlineAlert } from "../../../ui";
import type { CoworkDocumentSummary, CoworkFolderSummary } from "../contracts";
import { asCoworkApiError, coworkErrorMessage } from "../providers/errors";

interface CoworkDocumentPickerProps {
  readonly folder: CoworkFolderSummary;
  readonly documents: readonly CoworkDocumentSummary[];
  readonly currentDocumentId?: string;
  readonly onClose: () => void;
  readonly onOpen: (document: CoworkDocumentSummary) => Promise<void> | void;
  readonly onCreate: () => void;
  readonly onRegister: () => void;
  readonly onRepair: (document: CoworkDocumentSummary) => void;
  readonly onChangeFolder: () => void;
}

const isReady = (document: CoworkDocumentSummary): boolean =>
  (document.initializationState ?? "ready") === "ready" &&
  document.permissions?.open !== false &&
  document.lifecycle !== "retired";

export function CoworkDocumentPicker({
  folder,
  documents,
  currentDocumentId,
  onClose,
  onOpen,
  onCreate,
  onRegister,
  onRepair,
  onChangeFolder,
}: CoworkDocumentPickerProps) {
  const [query, setQuery] = useState("");
  const [openingId, setOpeningId] = useState<string | null>(null);
  const openingIdRef = useRef<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const normalized = query.trim().toLocaleLowerCase();
  const matching = useMemo(
    () =>
      documents.filter(
        (document) =>
          normalized.length === 0 ||
          document.title.toLocaleLowerCase().includes(normalized) ||
          document.path.toLocaleLowerCase().includes(normalized),
      ),
    [documents, normalized],
  );
  const ready = matching.filter(isReady);
  const needsAttention = matching.filter((document) => !isReady(document));

  const open = async (key: Key): Promise<void> => {
    const document = ready.find((entry) => entry.documentId === String(key));
    if (document === undefined || openingIdRef.current !== null) return;
    openingIdRef.current = document.documentId;
    setOpeningId(document.documentId);
    setError(null);
    try {
      await onOpen(document);
      onClose();
    } catch (openError) {
      openingIdRef.current = null;
      setOpeningId(null);
      setError(
        coworkErrorMessage(
          asCoworkApiError(openError),
          "Co-work couldn’t open that document.",
        ),
      );
    }
  };

  return (
    <ModalOverlay isOpen isDismissable={openingId === null} onOpenChange={(openState) => {
      if (!openState && openingId === null) onClose();
    }} className="wb-cowork-dialog-overlay">
      <Modal className="wb-cowork-dialog wb-cowork-dialog--picker">
        <Dialog aria-labelledby="cowork-picker-title" className="wb-cowork-dialog__body">
          <Heading id="cowork-picker-title" slot="title">Open document</Heading>
          <div className="wb-cowork-picker__context">
            <strong title={folder.folderPath}>{folder.folderName}</strong>
            <Button size="small" variant="ghost" onClick={onChangeFolder}>Change Folder</Button>
          </div>
          {error !== null ? <InlineAlert role="alert" tone="danger">{error}</InlineAlert> : null}
          <TextField value={query} onChange={setQuery} className="wb-cowork-field">
            <Label>Search documents</Label>
            <Input autoFocus placeholder="Search by title or relative path" />
          </TextField>
          {ready.length === 0 ? (
            <p className="wb-cowork-dialog__empty">No documents match this search.</p>
          ) : (
            <ListBox
              aria-label="Documents"
              selectionMode="single"
              selectedKeys={currentDocumentId === undefined ? [] : [currentDocumentId]}
              className="wb-cowork-picker__list"
            >
              {ready.map((document) => (
                <ListBoxItem
                  id={document.documentId}
                  key={document.documentId}
                  textValue={`${document.title} ${document.path}`}
                  onAction={() => void open(document.documentId)}
                  onKeyDown={(event) => {
                    if (event.key !== "Enter") return;
                    event.preventDefault();
                    void open(document.documentId);
                  }}
                  onPress={(event) => {
                    // A document picker should open on an ordinary mouse/touch press. Keep
                    // keyboard selection and activation distinct: Enter/Space continues
                    // through this item's React Aria `onAction` contract.
                    if (event.pointerType !== "keyboard") {
                      void open(document.documentId);
                    }
                  }}
                  className="wb-cowork-picker__item"
                >
                  <span className="wb-cowork-picker__copy">
                    <strong>{document.title}</strong>
                    <small>{document.path}</small>
                  </span>
                  <span className="wb-cowork-picker__signals">
                    {document.documentId === currentDocumentId ? "Open" : null}
                    {document.openProposalCount > 0
                      ? `${document.openProposalCount} proposal${document.openProposalCount === 1 ? "" : "s"}`
                      : null}
                    {document.driftState === "drifted" ? "Markdown changed" : null}
                  </span>
                </ListBoxItem>
              ))}
            </ListBox>
          )}
          {needsAttention.length > 0 ? (
            <section aria-labelledby="cowork-needs-attention-title" className="wb-cowork-picker__attention">
              <h3 id="cowork-needs-attention-title">Needs attention</h3>
              {needsAttention.map((document) => (
                <div key={document.documentId} className="wb-cowork-picker__attention-row">
                  <span><strong>{document.title}</strong><small>{document.path}</small></span>
                  <Button
                    size="small"
                    disabled={!document.permissions?.repair || openingId !== null}
                    title={document.disabledReason ?? "This document cannot be opened yet."}
                    onClick={() => onRepair(document)}
                  >
                    {document.permissions?.repair ? "Repair" : "Unavailable"}
                  </Button>
                </div>
              ))}
            </section>
          ) : null}
          <div className="wb-cowork-dialog__actions wb-cowork-picker__footer">
            <Button onClick={onClose} disabled={openingId !== null}>Close</Button>
            <Button onClick={onRegister} disabled={openingId !== null}>Add Markdown</Button>
            <Button variant="primary" onClick={onCreate} disabled={openingId !== null}>Create new document</Button>
          </div>
          {openingId !== null ? <p role="status">Opening document…</p> : null}
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
