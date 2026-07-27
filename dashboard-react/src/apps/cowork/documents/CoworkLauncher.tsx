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
import {
  CoworkLocalDocumentMetadata,
  coworkFolderDocumentMetadata,
} from "./CoworkDocumentMetadata";

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
  readonly onOpenLocalDocument: (document: CoworkScratchSummary) => void;
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

type LauncherDocument =
  | {
      readonly kind: "registered";
      readonly key: string;
      readonly sortAt: string | null;
      readonly document: CoworkDocumentSummary;
    }
  | {
      readonly kind: "local";
      readonly key: string;
      readonly sortAt: string;
      readonly document: CoworkScratchSummary;
    };

function Documents({
  registered,
  local,
  folderName,
  onOpenRegistered,
  onOpenLocal,
  disabled = false,
}: {
  readonly registered: readonly CoworkDocumentSummary[];
  readonly local: readonly CoworkScratchSummary[];
  readonly folderName: string | null;
  readonly onOpenRegistered: (document: CoworkDocumentSummary) => void;
  readonly onOpenLocal: (document: CoworkScratchSummary) => void;
  readonly disabled?: boolean;
}) {
  const documents: readonly LauncherDocument[] = [
    ...registered.map(
      (document): LauncherDocument => ({
        kind: "registered",
        key: `registered:${document.documentId}`,
        sortAt: document.updatedAt ?? null,
        document,
      }),
    ),
    ...local.map(
      (document): LauncherDocument => ({
        kind: "local",
        key: `local:${document.scratchId}`,
        sortAt: document.updatedAt,
        document,
      }),
    ),
  ].sort((left, right) => {
    if (left.sortAt === right.sortAt) return left.key.localeCompare(right.key);
    if (left.sortAt === null) return 1;
    if (right.sortAt === null) return -1;
    return right.sortAt.localeCompare(left.sortAt);
  });

  return (
    <section aria-labelledby="cowork-documents" className="wb-cowork-launcher__section">
      <h3 id="cowork-documents">Documents</h3>
      {documents.length === 0 ? (
        <p className="wb-cowork-launcher__empty">No documents yet.</p>
      ) : (
        <div className="wb-cowork-launcher__document-list">
          {documents.map((entry) => (
            <button
              key={entry.key}
              type="button"
              onClick={() => {
                if (entry.kind === "registered") {
                  onOpenRegistered(entry.document);
                } else {
                  onOpenLocal(entry.document);
                }
              }}
              className="wb-cowork-launcher__document"
              disabled={disabled}
            >
              <span className="wb-cowork-launcher__document-copy">
                <strong>{entry.document.title}</strong>
                <small>
                  {entry.kind === "registered"
                    ? folderName === null
                      ? entry.document.path
                      : coworkFolderDocumentMetadata(folderName, entry.document.path)
                    : <CoworkLocalDocumentMetadata document={entry.document} />}
                </small>
              </span>
              {entry.kind === "registered" &&
              entry.document.openProposalCount > 0 ? (
                <small className="wb-cowork-launcher__document-signal">
                  {entry.document.openProposalCount} open{" "}
                  {entry.document.openProposalCount === 1 ? "proposal" : "proposals"}
                </small>
              ) : null}
            </button>
          ))}
        </div>
      )}
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
  onOpenLocalDocument,
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
                  off read-only mode, then open this folder again.
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
    const readyDocuments = model.catalog.documents
      .filter(
        (document) =>
          (document.initializationState ?? "ready") === "ready" &&
          document.lifecycle !== "retired" &&
          document.permissions?.open !== false,
      );
    return (
      <section className="wb-cowork-launcher" aria-label={`${folder.folderName} documents`}>
        {model.navigationError !== null ? (
          <InlineAlert tone="danger">
            {coworkErrorMessage(
              model.navigationError,
              "Co-work couldn’t open that folder.",
            )}
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
        <Documents
          registered={readyDocuments}
          local={[]}
          folderName={folder.folderName}
          onOpenRegistered={onOpenDocument}
          onOpenLocal={onOpenLocalDocument}
          disabled={navigationBusy}
        />
      </section>
    );
  }

  return (
    <section className="wb-cowork-launcher" aria-label="Co-work workspace">
      {model.navigationError !== null ? (
        <InlineAlert tone="danger">
          {coworkErrorMessage(
            model.navigationError,
            "Co-work couldn’t open that folder.",
          )}
        </InlineAlert>
      ) : null}
      {!model.folderChooser.available ? (
        <InlineAlert tone="warning">
          Choosing a folder isn’t available here.
        </InlineAlert>
      ) : null}
      <Documents
        registered={[]}
        local={model.scratches}
        folderName={null}
        onOpenRegistered={onOpenDocument}
        onOpenLocal={onOpenLocalDocument}
        disabled={navigationBusy}
      />
      {model.folders.length > 0 ? (
        <section aria-labelledby="cowork-known-folders" className="wb-cowork-launcher__section">
          <h3 id="cowork-known-folders">Folders</h3>
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
    </section>
  );
}
