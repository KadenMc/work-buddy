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
import type { CoworkDocumentSummary, CoworkScratchSummary } from "../contracts";
import { asCoworkApiError, coworkErrorMessage } from "../providers/errors";

interface CoworkDocumentPickerProps {
  readonly documents: readonly CoworkDocumentSummary[];
  readonly localDocuments: readonly CoworkScratchSummary[];
  readonly currentDocumentId?: string;
  readonly currentLocalDocumentId?: string;
  readonly onClose: () => void;
  readonly onOpen: (document: CoworkDocumentSummary) => Promise<void> | void;
  readonly onOpenLocal: (document: CoworkScratchSummary) => Promise<void> | void;
  readonly onRepair: (document: CoworkDocumentSummary) => void;
}

const isReady = (document: CoworkDocumentSummary): boolean =>
  (document.initializationState ?? "ready") === "ready" &&
  document.permissions?.open !== false &&
  document.lifecycle !== "retired";

const localMetadata = (document: CoworkScratchSummary): string => {
  if (document.recoveredFromPreviousEditor) {
    return "Recovered from an earlier session · Not saved to a Folder";
  }
  const edited = document.updatedAt !== document.createdAt;
  const activityAt = new Date(edited ? document.updatedAt : document.createdAt);
  if (Number.isNaN(activityAt.getTime())) return "Not saved to a Folder";
  return `Not saved to a Folder · ${edited ? "Edited" : "Created"} ${new Intl.DateTimeFormat(
    undefined,
    {
      dateStyle: "medium",
      timeStyle: "short",
    },
  ).format(activityAt)}`;
};

type PickerDocument =
  | {
      readonly kind: "registered";
      readonly key: string;
      readonly document: CoworkDocumentSummary;
    }
  | {
      readonly kind: "local";
      readonly key: string;
      readonly document: CoworkScratchSummary;
    };

export function CoworkDocumentPicker({
  documents,
  localDocuments,
  currentDocumentId,
  currentLocalDocumentId,
  onClose,
  onOpen,
  onOpenLocal,
  onRepair,
}: CoworkDocumentPickerProps) {
  const [query, setQuery] = useState("");
  const [openingKey, setOpeningKey] = useState<string | null>(null);
  const openingKeyRef = useRef<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const normalized = query.trim().toLocaleLowerCase();
  const matchingRegistered = useMemo(
    () => documents.filter(
      (document) =>
        normalized.length === 0 ||
        document.title.toLocaleLowerCase().includes(normalized) ||
        document.path.toLocaleLowerCase().includes(normalized),
    ),
    [documents, normalized],
  );
  const matchingLocal = useMemo(
    () => localDocuments.filter(
      (document) =>
        normalized.length === 0 ||
        document.title.toLocaleLowerCase().includes(normalized),
    ),
    [localDocuments, normalized],
  );
  const ready: readonly PickerDocument[] = [
    ...matchingRegistered
      .filter(isReady)
      .map(
        (document): PickerDocument => ({
          kind: "registered",
          key: `registered:${document.documentId}`,
          document,
        }),
      ),
    ...matchingLocal.map(
      (document): PickerDocument => ({
        kind: "local",
        key: `local:${document.scratchId}`,
        document,
      }),
    ),
  ];
  const needsAttention = matchingRegistered.filter((document) => !isReady(document));
  const selectedKey =
    currentDocumentId !== undefined
      ? `registered:${currentDocumentId}`
      : currentLocalDocumentId !== undefined
        ? `local:${currentLocalDocumentId}`
        : null;

  const open = async (key: Key): Promise<void> => {
    const entry = ready.find((candidate) => candidate.key === String(key));
    if (entry === undefined || openingKeyRef.current !== null) return;
    openingKeyRef.current = entry.key;
    setOpeningKey(entry.key);
    setError(null);
    try {
      if (entry.kind === "registered") {
        await onOpen(entry.document);
      } else {
        await onOpenLocal(entry.document);
      }
      onClose();
    } catch (openError) {
      openingKeyRef.current = null;
      setOpeningKey(null);
      setError(
        coworkErrorMessage(
          asCoworkApiError(openError),
          "Co-work couldn’t open that document.",
        ),
      );
    }
  };

  return (
    <ModalOverlay isOpen isDismissable={openingKey === null} onOpenChange={(openState) => {
      if (!openState && openingKey === null) onClose();
    }} className="wb-cowork-dialog-overlay">
      <Modal className="wb-cowork-dialog wb-cowork-dialog--picker">
        <Dialog aria-labelledby="cowork-picker-title" className="wb-cowork-dialog__body">
          <Heading id="cowork-picker-title" slot="title">Open document</Heading>
          {error !== null ? <InlineAlert role="alert" tone="danger">{error}</InlineAlert> : null}
          <TextField value={query} onChange={setQuery} className="wb-cowork-field">
            <Label>Search documents</Label>
            <Input autoFocus placeholder="Search by title or relative path" />
          </TextField>
          {ready.length === 0 ? (
            <p className="wb-cowork-dialog__empty">
              {normalized.length > 0
                ? "No documents match this search."
                : needsAttention.length > 0
                  ? "No documents are ready to open."
                  : "No documents yet."}
            </p>
          ) : (
            <ListBox
              aria-label="Documents"
              selectionMode="single"
              selectedKeys={selectedKey === null ? [] : [selectedKey]}
              className="wb-cowork-picker__list"
            >
              {ready.map((entry) => (
                <ListBoxItem
                  id={entry.key}
                  key={entry.key}
                  textValue={`${entry.document.title} ${
                    entry.kind === "registered"
                      ? entry.document.path
                      : localMetadata(entry.document)
                  }`}
                  onAction={() => void open(entry.key)}
                  onKeyDown={(event) => {
                    if (event.key !== "Enter") return;
                    event.preventDefault();
                    void open(entry.key);
                  }}
                  onPress={(event) => {
                    // A document picker should open on an ordinary mouse/touch press. Keep
                    // keyboard selection and activation distinct: Enter/Space continues
                    // through this item's React Aria `onAction` contract.
                    if (event.pointerType !== "keyboard") {
                      void open(entry.key);
                    }
                  }}
                  className="wb-cowork-picker__item"
                >
                  <span className="wb-cowork-picker__copy">
                    <strong>{entry.document.title}</strong>
                    <small>
                      {entry.kind === "registered"
                        ? entry.document.path
                        : localMetadata(entry.document)}
                    </small>
                  </span>
                  <span className="wb-cowork-picker__signals">
                    {entry.key === selectedKey ? "Open" : null}
                    {entry.kind === "registered" &&
                    entry.document.openProposalCount > 0
                      ? `${entry.document.openProposalCount} proposal${
                          entry.document.openProposalCount === 1 ? "" : "s"
                        }`
                      : null}
                    {entry.kind === "registered" &&
                    entry.document.driftState === "drifted"
                      ? "Markdown changed"
                      : null}
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
                    disabled={!document.permissions?.repair || openingKey !== null}
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
            <Button onClick={onClose} disabled={openingKey !== null}>Close</Button>
          </div>
          {openingKey !== null ? <p role="status">Opening document…</p> : null}
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
