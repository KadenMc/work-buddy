import { NotePencil } from "@phosphor-icons/react/NotePencil";
import {
  Dialog,
  Heading,
  Modal,
  ModalOverlay,
} from "react-aria-components";

import { Button, InlineAlert, Spinner } from "../../../ui";
import type {
  CoworkDocumentSummary,
  CoworkFolderSummary,
  CoworkScratchSummary,
  CoworkViewModel,
} from "../contracts";
import { coworkErrorMessage } from "../providers/errors";

interface CoworkLauncherProps {
  readonly model: CoworkViewModel;
  readonly pendingFolderAction?: {
    readonly action: string;
    readonly storeId?: string;
  } | null;
  readonly onRetryInspection: () => void;
  readonly onCancelInspection: () => void;
  readonly onInitialize: () => void;
  readonly onOpenFolder: (storeId: string) => void;
  readonly onOpenDocument: (document: CoworkDocumentSummary) => void;
  readonly onRegister: () => void;
  readonly onOpenLocalDocument: (document: CoworkScratchSummary) => void;
  readonly onNewDocument: () => void;
}

const unavailableCopy: Record<string, string> = {
  folder_not_found: "That folder no longer exists.",
  folder_unreadable: "Work Buddy can’t read that folder.",
  folder_disallowed: "Work Buddy can’t open that location.",
  descendant_scan_incomplete: "Co-work couldn’t finish opening that folder.",
  folder_too_large_for_safe_setup: "Choose a smaller folder to use with Co-work.",
};

const folderConflictCopy: Record<string, string> = {
  folder_layout_incomplete: "This folder has an incomplete Co-work setup.",
  folder_store_collision: "This folder is already connected from another location.",
  identity_conflict: "This folder doesn’t match the Co-work data registered for it.",
};

const folderFor = (model: CoworkViewModel): CoworkFolderSummary | null =>
  model.folderSelection.kind === "initialized"
    ? model.folderSelection.folder
    : model.folders.find((entry) => entry.storeId === model.activeFolderStoreId) ?? null;

const navigationMessage = (model: CoworkViewModel, fallback: string): string =>
  model.navigationError === null
    ? fallback
    : coworkErrorMessage(model.navigationError, fallback);

const parentSegments = (folderPath: string): readonly string[] =>
  folderPath
    .replace(/[\\/]+$/, "")
    .split(/[\\/]+/)
    .slice(0, -1)
    .filter((segment) => segment.length > 0);

const duplicateFolderContext = (
  folder: CoworkFolderSummary,
  folders: readonly CoworkFolderSummary[],
): string | null => {
  const duplicates = folders.filter(
    (candidate) =>
      candidate.folderName.toLocaleLowerCase() ===
      folder.folderName.toLocaleLowerCase(),
  );
  if (duplicates.length < 2) return null;
  const target = parentSegments(folder.folderPath);
  const parents = duplicates.map((candidate) => parentSegments(candidate.folderPath));
  for (let depth = 1; depth <= target.length; depth += 1) {
    const label = target.slice(-depth).join(" / ");
    const unique = parents.filter(
      (segments) => segments.slice(-depth).join(" / ") === label,
    ).length === 1;
    if (unique) return label;
  }
  return target.join(" / ") || folder.folderPath;
};

const activityLabel = (document: CoworkScratchSummary): string => {
  const edited = document.updatedAt !== document.createdAt;
  const activityAt = new Date(edited ? document.updatedAt : document.createdAt);
  if (Number.isNaN(activityAt.getTime())) return "Not saved to a folder yet";
  return `${edited ? "Edited" : "Created"} ${new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(activityAt)}`;
};

function LocalDocuments({
  documents,
  onOpen,
  disabled = false,
}: {
  readonly documents: readonly CoworkScratchSummary[];
  readonly onOpen: (document: CoworkScratchSummary) => void;
  readonly disabled?: boolean;
}) {
  if (documents.length === 0) return null;
  return (
    <section aria-labelledby="cowork-local-documents" className="wb-cowork-launcher__section">
      <h3 id="cowork-local-documents">On this device</h3>
      {documents.map((document) => (
        <div key={document.scratchId} className="wb-cowork-launcher__local-document">
          <span>
            <strong>{document.title}</strong>
            <small>
              {document.recoveredFromPreviousEditor
                ? "Recovered from an earlier session"
                : activityLabel(document)}
            </small>
          </span>
          <Button size="small" onClick={() => onOpen(document)} disabled={disabled}>
            Continue
          </Button>
        </div>
      ))}
    </section>
  );
}

export function CoworkLauncher({
  model,
  pendingFolderAction = null,
  onRetryInspection,
  onCancelInspection,
  onInitialize,
  onOpenFolder,
  onOpenDocument,
  onRegister,
  onOpenLocalDocument,
  onNewDocument,
}: CoworkLauncherProps) {
  const selection = model.folderSelection;
  const folder = folderFor(model);
  const openingFolder = selection.kind === "choosing" || selection.kind === "inspecting";
  const canReturnToPreviousContext =
    model.activeSession.kind !== "none" || model.activeFolderStoreId !== null;
  const returnLabel =
    model.activeSession.kind === "none" ? "Back" : "Back to document";
  const folderActionBusy = pendingFolderAction !== null;
  const navigationBusy = openingFolder || folderActionBusy;

  if (selection.kind === "inspecting_descendants") {
    return (
      <section
        className="wb-cowork-launcher wb-cowork-launcher--centered"
        aria-label="Opening folder"
        aria-busy="true"
      >
        <Spinner />
        <p>Opening {selection.candidate.folderName}…</p>
        <Button onClick={onCancelInspection}>Cancel</Button>
      </section>
    );
  }

  if (selection.kind === "setup_confirmation") {
    return (
      <ModalOverlay
        isOpen
        isDismissable={false}
        className="wb-cowork-dialog-overlay"
      >
        <Modal className="wb-cowork-dialog">
          <Dialog
            aria-labelledby="cowork-setup-title"
            aria-describedby="cowork-setup-description"
            className="wb-cowork-dialog__body"
          >
            <Heading id="cowork-setup-title" slot="title">
              {model.readOnly
                ? `Co-work isn’t set up in “${selection.candidate.folderName}”`
                : `Set up Co-work in “${selection.candidate.folderName}”?`}
            </Heading>
            <p id="cowork-setup-description">
              {model.readOnly ? (
                <>
                  This dashboard is read-only, so Co-work can’t add its support
                  data under <code>.wbuddy</code>. No files were changed. Turn
                  off read-only mode, then open this Folder again.
                </>
              ) : (
                <>
                  This adds Co-work support data under <code>.wbuddy</code>. Your
                  documents won’t be changed.
                </>
              )}
            </p>
            <p
              className="wb-cowork-dialog__selection"
              title={selection.candidate.folderPath}
            >
              <strong>{selection.candidate.folderName}</strong>
              <span>{selection.candidate.folderPath}</span>
            </p>
            <div className="wb-cowork-dialog__actions">
              <Button
                autoFocus
                onClick={onCancelInspection}
                disabled={folderActionBusy}
              >
                {model.readOnly ? "Close" : "Cancel"}
              </Button>
              {!model.readOnly ? (
                <Button
                  variant="primary"
                  onClick={onInitialize}
                  disabled={folderActionBusy}
                >
                  {folderActionBusy ? "Setting up…" : "Set up Co-work"}
                </Button>
              ) : null}
            </div>
          </Dialog>
        </Modal>
      </ModalOverlay>
    );
  }

  if (selection.kind === "setup_available") {
    return (
      <section className="wb-cowork-launcher wb-cowork-launcher--centered">
        {!folderActionBusy ? (
          <InlineAlert tone="danger">
            {navigationMessage(
              model,
              `Co-work couldn’t finish opening ${selection.candidate.folderName}.`,
            )}
          </InlineAlert>
        ) : null}
        <div className="wb-cowork-launcher__actions">
          <Button onClick={onCancelInspection} disabled={folderActionBusy}>
            {canReturnToPreviousContext ? returnLabel : "Cancel"}
          </Button>
          <Button variant="primary" onClick={onInitialize} disabled={folderActionBusy}>
            {folderActionBusy ? "Setting up…" : "Try again"}
          </Button>
        </div>
      </section>
    );
  }

  if (selection.kind === "inside_existing_folder") {
    return (
      <section className="wb-cowork-launcher wb-cowork-launcher--centered">
        <InlineAlert tone="warning">
          {navigationMessage(
            model,
            `That location is inside ${selection.owner.folderName}.`,
          )}
        </InlineAlert>
        <div className="wb-cowork-launcher__actions">
          {canReturnToPreviousContext ? (
            <Button onClick={onCancelInspection} disabled={folderActionBusy}>
              {returnLabel}
            </Button>
          ) : null}
          <Button
            variant="primary"
            onClick={() => onOpenFolder(selection.owner.storeId)}
            disabled={folderActionBusy}
          >
            {folderActionBusy ? "Opening…" : `Open ${selection.owner.folderName}`}
          </Button>
        </div>
      </section>
    );
  }

  if (selection.kind === "contains_nested_folder") {
    return (
      <section className="wb-cowork-launcher wb-cowork-launcher--centered">
        <InlineAlert tone="warning">
          {navigationMessage(
            model,
            "Choose a smaller folder. This one already contains a Co-work folder.",
          )}
        </InlineAlert>
        <ul className="wb-cowork-launcher__boundaries">
          {selection.boundaries.map((boundary) => (
            <li key={boundary.folderPath} title={boundary.folderPath}>
              {boundary.folderName}
            </li>
          ))}
        </ul>
        {canReturnToPreviousContext ? (
          <Button onClick={onCancelInspection}>{returnLabel}</Button>
        ) : null}
      </section>
    );
  }

  if (selection.kind === "store_layout_conflict") {
    return (
      <section className="wb-cowork-launcher wb-cowork-launcher--centered">
        {!folderActionBusy ? (
          <InlineAlert tone="danger">
            {navigationMessage(
              model,
              folderConflictCopy[selection.reasonCode] ??
                "Co-work found folder data it can’t open safely.",
            )}
          </InlineAlert>
        ) : null}
        <div className="wb-cowork-launcher__actions">
          {canReturnToPreviousContext ? (
            <Button onClick={onCancelInspection} disabled={folderActionBusy}>
              {returnLabel}
            </Button>
          ) : null}
          {selection.availableActions.includes("retry") ? (
            <Button onClick={onRetryInspection} disabled={folderActionBusy}>
              {folderActionBusy ? "Opening…" : "Try again"}
            </Button>
          ) : null}
        </div>
      </section>
    );
  }

  if (selection.kind === "unavailable") {
    return (
      <section className="wb-cowork-launcher wb-cowork-launcher--centered">
        {!folderActionBusy ? (
          <InlineAlert tone="danger">
            {navigationMessage(
              model,
              unavailableCopy[selection.reasonCode] ??
                "Co-work can’t open that folder.",
            )}
          </InlineAlert>
        ) : null}
        <div className="wb-cowork-launcher__actions">
          {canReturnToPreviousContext ? (
            <Button onClick={onCancelInspection} disabled={folderActionBusy}>
              {returnLabel}
            </Button>
          ) : null}
          {selection.retryable ? (
            <Button onClick={onRetryInspection} disabled={folderActionBusy}>
              {folderActionBusy ? "Opening…" : "Try again"}
            </Button>
          ) : null}
        </div>
      </section>
    );
  }

  if (folder !== null) {
    const markdownPickerUnavailable = !model.folderChooser.markdownAvailable;
    const readyDocuments = model.catalog.documents
      .filter((document) => (document.initializationState ?? "ready") === "ready")
      .reverse()
      .sort((left, right) => {
        if (left.updatedAt === right.updatedAt) return 0;
        if (left.updatedAt == null) return 1;
        if (right.updatedAt == null) return -1;
        return right.updatedAt.localeCompare(left.updatedAt);
      })
      .slice(0, 6);
    return (
      <section className="wb-cowork-launcher" aria-label={`${folder.folderName} documents`}>
        {model.navigationError !== null ? (
          <InlineAlert tone="danger">
            {coworkErrorMessage(
              model.navigationError,
              "Co-work couldn’t open that Folder.",
            )}
          </InlineAlert>
        ) : null}
        <div className="wb-cowork-launcher__primary-actions">
          <Button
            onClick={onRegister}
            disabled={
              navigationBusy ||
              !folder.permissions.import ||
              markdownPickerUnavailable
            }
            aria-describedby={
              markdownPickerUnavailable
                ? "cowork-launcher-markdown-picker-unavailable"
                : undefined
            }
            title={
              markdownPickerUnavailable
                ? "Markdown file selection isn’t available here."
                : "Create a new document from an existing Markdown file."
            }
          >
            New from Markdown
          </Button>
        </div>
        {markdownPickerUnavailable ? (
          <InlineAlert
            id="cowork-launcher-markdown-picker-unavailable"
            tone="warning"
          >
            Markdown file selection isn’t available here.
          </InlineAlert>
        ) : null}
        {model.catalog.status === "loading" ? (
          <p role="status" className="wb-cowork-launcher__loading">
            <Spinner /> Loading documents…
          </p>
        ) : null}
        {model.catalog.error !== null ? (
          <InlineAlert tone="danger">
            {coworkErrorMessage(
              model.catalog.error,
              "Co-work couldn’t load the documents.",
            )}{" "}
            <Button size="small" onClick={onRetryInspection}>
              Try again
            </Button>
          </InlineAlert>
        ) : null}
        {readyDocuments.length > 0 ? (
          <section aria-labelledby="cowork-recent-documents" className="wb-cowork-launcher__section">
            <h3 id="cowork-recent-documents">Recent documents</h3>
            <div className="wb-cowork-launcher__cards">
              {readyDocuments.map((document) => (
                <button
                  key={document.documentId}
                  type="button"
                  onClick={() => onOpenDocument(document)}
                  className="wb-cowork-launcher__card"
                  disabled={navigationBusy}
                >
                  <strong>{document.title}</strong>
                  <span>{document.path}</span>
                  {document.openProposalCount > 0 ? (
                    <small>{document.openProposalCount} open proposals</small>
                  ) : null}
                </button>
              ))}
            </div>
          </section>
        ) : null}
        <LocalDocuments
          documents={model.scratches}
          onOpen={onOpenLocalDocument}
          disabled={navigationBusy}
        />
      </section>
    );
  }

  return (
    <section className="wb-cowork-launcher wb-cowork-launcher--start" aria-label="Start a document">
      {model.navigationError !== null ? (
        <InlineAlert tone="danger">
          {coworkErrorMessage(
            model.navigationError,
            "Co-work couldn’t open that Folder.",
          )}
        </InlineAlert>
      ) : null}
      <div className="wb-cowork-launcher__primary-actions">
        <Button variant="primary" onClick={onNewDocument} disabled={navigationBusy}>
          <NotePencil aria-hidden="true" /> New document
        </Button>
      </div>
      {!model.folderChooser.available ? (
        <InlineAlert tone="warning">
          Folder selection isn’t available here.
        </InlineAlert>
      ) : null}
      {model.folders.length > 0 ? (
        <section aria-labelledby="cowork-known-folders" className="wb-cowork-launcher__section">
          <h3 id="cowork-known-folders">Recent folders</h3>
          <div className="wb-cowork-launcher__cards">
            {model.folders.map((known) => (
              <button
                key={known.storeId}
                type="button"
                title={known.folderPath}
                onClick={() => onOpenFolder(known.storeId)}
                className="wb-cowork-launcher__card"
                disabled={navigationBusy}
              >
                <strong>{known.folderName}</strong>
                {pendingFolderAction?.storeId === known.storeId ? (
                  <small>Opening…</small>
                ) : duplicateFolderContext(known, model.folders) !== null ? (
                  <small>{duplicateFolderContext(known, model.folders)}</small>
                ) : null}
              </button>
            ))}
          </div>
        </section>
      ) : null}
      <LocalDocuments
        documents={model.scratches}
        onOpen={onOpenLocalDocument}
        disabled={navigationBusy}
      />
    </section>
  );
}
