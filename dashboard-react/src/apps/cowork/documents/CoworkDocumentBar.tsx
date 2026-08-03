import { useEffect, useRef } from "react";
import { DotsThree } from "@phosphor-icons/react/DotsThree";
import { FolderSimple } from "@phosphor-icons/react/FolderSimple";
import { NotePencil } from "@phosphor-icons/react/NotePencil";
import { X } from "@phosphor-icons/react/X";
import { Menu, MenuItem, MenuTrigger, Popover, type Key } from "react-aria-components";

import { Button } from "../../../ui";
import {
  coworkDocumentCanWriteBackSource,
  type CoworkDocumentSummary,
  type CoworkFolderSummary,
  type CoworkViewModel,
} from "../contracts";
import type { CoworkSyncStatus } from "../persistence/CoworkYdocPersistence";
import type { CoworkMaterializationState } from "../materialization/contracts";
import { coworkErrorMessage } from "../providers/errors";

interface CoworkDocumentBarProps {
  readonly model: CoworkViewModel;
  readonly onChooseFolder: () => void;
  readonly onCloseFolder: () => void;
  readonly folderActionBusy?: boolean;
  readonly creationActionsBusy?: boolean;
  readonly closingFolder?: boolean;
  readonly onOpenPicker: () => void;
  readonly onCreate: () => void;
  readonly onImportFile: () => void;
  readonly onCloseSession: () => void;
  readonly onPromoteScratch: () => void;
  readonly promotionBusy?: boolean;
  readonly promotionReady?: boolean;
  readonly syncStatus?: CoworkSyncStatus;
  readonly materializationState?: CoworkMaterializationState;
  readonly onSaveMarkdown?: () => void;
  readonly onRetrySync?: () => void;
  readonly onReviewExternalChanges?: () => void;
  readonly onRemoveDocument?: () => void;
  readonly onDiscardLocalDocument?: () => void;
}

export const coworkReimportLocalBlockedReason = (
  syncStatus?: CoworkSyncStatus,
  materializationState?: CoworkMaterializationState,
): string | null => {
  if (syncStatus !== "clean") {
    return "Sync this document to Co-work before reviewing external changes.";
  }
  if (materializationState?.kind === "up_to_date") return null;
  // A stale-file conflict is the expected projection state for external drift. The dialog's
  // authoritative drift inspection additionally requires `unmaterialized_structured_edits`
  // to be false before either prepare or commit can proceed.
  if (
    materializationState?.kind === "conflict" &&
    materializationState.error.code === "stale_file"
  ) {
    return null;
  }
  return "Save the current Co-work edits before reviewing external changes.";
};

export const coworkScratchPromotionBlockedReason = (
  model: Pick<CoworkViewModel, "readOnly" | "folderChooser">,
  folder: CoworkFolderSummary | null,
): string | null => {
  if (model.readOnly) {
    return "Read-only mode. This document will stay in this browser.";
  }
  if (folder === null && !model.folderChooser.available) {
    return "Choosing a folder isn’t available here. This document will stay in this browser.";
  }
  if (folder !== null && !folder.permissions.create) {
    return "This folder doesn’t allow new documents. This document will stay in this browser.";
  }
  return null;
};

export const coworkImportBlockedReason = (
  model: Pick<CoworkViewModel, "readOnly" | "folderChooser">,
  folder: CoworkFolderSummary | null,
): string | null =>
  model.readOnly
    ? "Read-only mode. New folder documents aren’t available."
    : folder === null && !model.folderChooser.available
      ? "Choosing a folder isn’t available here."
      : folder !== null && !folder.permissions.import
        ? "This folder doesn’t allow file imports."
        : !model.folderChooser.importAvailable
          ? "File import isn’t available here."
          : null;

const registeredStatusLabel = (
  model: CoworkViewModel,
  document: CoworkDocumentSummary,
  syncStatus?: CoworkSyncStatus,
  state?: CoworkMaterializationState,
): string => {
  const sync = syncStatus ?? (model.readOnly ? "read_only" : "hydrating");
  if (sync === "read_only") return "Read-only";
  if (sync === "hydrating" || state === undefined || state.kind === "checking") {
    return "Loading…";
  }
  if (
    sync === "saving" ||
    sync === "retrying" ||
    state.kind === "saving"
  ) {
    return "Saving…";
  }
  if (sync === "saved_on_device" || sync === "offline") return "Saved in this browser";
  if (sync === "conflict") return "Sync conflict";
  if (sync === "error") return "Couldn’t save";
  if (document.sourceWriteback === "never") return "Saved in Co-work";
  if (state.kind === "read_only") return "Read-only";
  if (state.kind === "unsaved") return "Unsaved changes";
  if (state.kind === "conflict") {
    return state.error.code === "stale_file" ? "File changed outside Co-work" : "Save conflict";
  }
  if (state.kind === "error") return "Couldn’t save";
  return "Saved";
};

const localStatusLabel = (syncStatus?: CoworkSyncStatus): string => {
  const sync = syncStatus ?? "hydrating";
  if (sync === "hydrating") return "Loading…";
  if (sync === "saving" || sync === "retrying") return "Saving…";
  if (sync === "offline" || sync === "error") return "Couldn’t save in this browser";
  if (sync === "conflict") return "Save conflict";
  if (sync === "read_only") return "Read-only";
  return "Saved in this browser";
};

export function CoworkDocumentBar({
  model,
  onChooseFolder,
  onCloseFolder,
  folderActionBusy = false,
  creationActionsBusy = folderActionBusy,
  closingFolder = false,
  onOpenPicker,
  onCreate,
  onImportFile,
  onCloseSession,
  onPromoteScratch,
  promotionBusy = false,
  promotionReady = false,
  syncStatus,
  materializationState,
  onSaveMarkdown,
  onRetrySync,
  onReviewExternalChanges,
  onRemoveDocument,
  onDiscardLocalDocument,
}: CoworkDocumentBarProps) {
  const folder =
    model.folderSelection.kind === "initialized"
      ? model.folderSelection.folder
      : model.folders.find((entry) => entry.storeId === model.activeFolderStoreId) ?? null;
  const document = model.activeSession.kind === "registered"
    ? model.activeSession.document
    : null;
  const scratch = model.activeSession.kind === "scratch" ? model.activeSession : null;
  const openingFolder =
    model.folderSelection.kind === "choosing" ||
    model.folderSelection.kind === "inspecting" ||
    model.folderSelection.kind === "inspecting_descendants";
  const folderControlBusy = folderActionBusy || openingFolder;
  const reimportBlockedReason = coworkReimportLocalBlockedReason(
    syncStatus,
    materializationState,
  );
  const scratchPromotionBlockedReason = coworkScratchPromotionBlockedReason(
    model,
    folder,
  );
  const canRemove =
    !folderActionBusy &&
    onRemoveDocument !== undefined &&
    document?.permissions?.retire !== false &&
    syncStatus === "clean" &&
    (document?.sourceWriteback === "never" ||
      (document?.driftState === "clean" &&
        materializationState?.kind === "up_to_date"));
  const canOpenDocuments = folder !== null || model.scratches.length > 0;
  const canWriteBackSource =
    document !== null && coworkDocumentCanWriteBackSource(document);
  const createBlockedReason =
    folder === null
      ? null
      : model.readOnly
        ? "Read-only mode. New folder documents aren’t available."
        : !folder.permissions.create
          ? "This folder doesn’t allow new documents."
          : null;
  const importBlockedReason = coworkImportBlockedReason(model, folder);
  const folderTriggerRef = useRef<HTMLButtonElement>(null);
  const hadFolderRef = useRef(folder !== null);

  useEffect(() => {
    if (hadFolderRef.current && folder === null) {
      folderTriggerRef.current?.focus();
    }
    hadFolderRef.current = folder !== null;
  }, [folder]);

  return (
    <header className="wb-cowork__document-bar" aria-label="Co-work document controls">
      <div className="wb-cowork__document-context">
        <div
          className="wb-cowork__folder-control"
          aria-busy={closingFolder || undefined}
        >
          <Button
            ref={folderTriggerRef}
            variant="ghost"
            className="wb-cowork__folder-trigger"
            onClick={onChooseFolder}
            title={folder?.folderPath ?? "Open folder"}
            disabled={!model.folderChooser.available || folderControlBusy}
          >
            <FolderSimple weight="duotone" aria-hidden="true" />
            <span>{openingFolder ? "Opening…" : folder?.folderName ?? "Open folder"}</span>
          </Button>
          {folder !== null ? (
            <Button
              size="small"
              variant="ghost"
              className="wb-cowork__folder-close"
              onClick={onCloseFolder}
              disabled={folderControlBusy}
              title="Close this folder without removing it or changing its files."
            >
              <X aria-hidden="true" />
              <span>{closingFolder ? "Closing…" : "Close folder"}</span>
            </Button>
          ) : null}
          {closingFolder ? (
            <span className="wb-visually-hidden" role="status">
              Closing folder…
            </span>
          ) : null}
        </div>

        <Button
          variant="ghost"
          className="wb-cowork__document-trigger"
          onClick={onOpenPicker}
          disabled={!canOpenDocuments || folderControlBusy}
          title={
            document?.sourceWriteback === "never"
              ? `Imported from ${document.path}. Co-work will not change the source file.`
              : document?.path ??
            (scratch === null ? "Open document" : "Saved in this browser")
          }
        >
          <span>{document?.title ?? scratch?.title ?? "Open document"}</span>
          {document !== null ? (
            <small>
              {document.sourceWriteback === "never"
                ? `Import source: ${document.path}`
                : document.path}
            </small>
          ) : null}
        </Button>
      </div>

      <div className="wb-cowork__document-actions">
        <span className="wb-cowork__sync-status" role="status" aria-live="polite">
          {model.openingTarget !== null
            ? "Loading document…"
            : document !== null
              ? registeredStatusLabel(
                  model,
                  document,
                  syncStatus,
                  materializationState,
                )
              : scratch !== null
                ? localStatusLabel(syncStatus)
                : ""}
        </span>
        {document?.driftState === "drifted" ? (
          <Button
            size="small"
            onClick={onReviewExternalChanges}
            disabled={
              folderActionBusy ||
              onReviewExternalChanges === undefined ||
              reimportBlockedReason !== null
            }
            aria-describedby={
              reimportBlockedReason === null ? undefined : "cowork-reimport-blocked-reason"
            }
            title={reimportBlockedReason ?? "Compare and safely replace from Markdown."}
          >
            Review file changes
          </Button>
        ) : null}
        {document?.driftState === "drifted" && reimportBlockedReason !== null ? (
          <span
            id="cowork-reimport-blocked-reason"
            className="wb-cowork__save-message"
          >
            {reimportBlockedReason}
          </span>
        ) : null}
        {scratch !== null ? (
          <>
            <Button
              size="small"
              variant="primary"
              onClick={onPromoteScratch}
              disabled={
                folderActionBusy ||
                promotionBusy ||
                !promotionReady ||
                scratchPromotionBlockedReason !== null
              }
              aria-describedby={
                scratchPromotionBlockedReason === null
                  ? undefined
                  : "cowork-promotion-blocked-reason"
              }
              title={
                scratchPromotionBlockedReason ??
                (promotionReady
                  ? "Choose a folder and save this document."
                  : "The document is still loading.")
              }
            >
              {promotionBusy
                ? "Saving…"
                : promotionReady
                  ? "Save document"
                  : "Loading editor…"}
            </Button>
            {scratchPromotionBlockedReason !== null ? (
              <span
                id="cowork-promotion-blocked-reason"
                className="wb-cowork__save-message"
              >
                {scratchPromotionBlockedReason}
              </span>
            ) : null}
          </>
        ) : canWriteBackSource ? (
          syncStatus === "clean" ? (
            <Button
              size="small"
              variant="primary"
              onClick={onSaveMarkdown}
              disabled={
                onSaveMarkdown === undefined ||
                materializationState === undefined ||
                materializationState.kind === "checking" ||
                materializationState.kind === "up_to_date" ||
                materializationState.kind === "saving" ||
                materializationState.kind === "read_only" ||
                ((materializationState.kind === "conflict" ||
                  materializationState.kind === "error") &&
                  !materializationState.canRetry)
              }
            >
              {materializationState?.kind === "saving"
                ? "Saving…"
                : (materializationState?.kind === "conflict" ||
                      materializationState?.kind === "error") &&
                    materializationState.canRetry
                  ? "Try saving again"
                  : "Save"}
            </Button>
          ) : null
        ) : null}
        <Button
          size="small"
          onClick={() => {
            if (importBlockedReason !== null) return;
            onImportFile();
          }}
          disabled={creationActionsBusy}
          aria-disabled={importBlockedReason !== null || undefined}
          aria-describedby={
            importBlockedReason === null
              ? undefined
              : "cowork-new-from-markdown-blocked-reason"
          }
          title={
            importBlockedReason ??
            "Import a supported file into a new Co-work document. Markdown is supported today."
          }
        >
          From file
        </Button>
        {importBlockedReason !== null ? (
          <span
            id="cowork-new-from-markdown-blocked-reason"
            className="wb-visually-hidden"
          >
            {importBlockedReason}
          </span>
        ) : null}
        <Button
          size="small"
          variant="primary"
          onClick={() => {
            if (createBlockedReason !== null) return;
            onCreate();
          }}
          disabled={creationActionsBusy}
          aria-disabled={createBlockedReason !== null || undefined}
          aria-describedby={
            createBlockedReason === null ? undefined : "cowork-new-blocked-reason"
          }
          title={
            createBlockedReason ??
            (folder === null
              ? "Start a document and choose where to save it later."
              : "Create a document in this folder.")
          }
        >
          <NotePencil aria-hidden="true" /> New
        </Button>
        {createBlockedReason !== null ? (
          <span id="cowork-new-blocked-reason" className="wb-visually-hidden">
            {createBlockedReason}
          </span>
        ) : null}
        {(document !== null || (scratch !== null && promotionReady)) &&
        (syncStatus === "offline" ||
          syncStatus === "error" ||
          syncStatus === "conflict") ? (
          <Button size="small" onClick={onRetrySync} disabled={onRetrySync === undefined}>
            {scratch !== null ? "Try saving again" : "Sync now"}
          </Button>
        ) : null}
        {document !== null || scratch !== null ? (
          <Button
            size="small"
            variant="ghost"
            onClick={onCloseSession}
            aria-label="Close document"
            disabled={folderActionBusy}
          >
            <X aria-hidden="true" /> Close document
          </Button>
        ) : null}
        {document !== null || scratch !== null ? (
          <MenuTrigger>
            <Button
              size="small"
              variant="ghost"
              aria-label="More document actions"
              title="More document actions"
              disabled={folderActionBusy}
            >
              <DotsThree weight="bold" aria-hidden="true" />
            </Button>
            <Popover className="wb-popover" placement="bottom end">
              <Menu
                aria-label="More document actions"
                className="wb-action-menu"
                onAction={(key: Key) => {
                  if (String(key) === "remove") onRemoveDocument?.();
                  if (String(key) === "discard-local") onDiscardLocalDocument?.();
                }}
              >
                {document !== null ? (
                  <MenuItem
                    id="remove"
                    className="wb-action-menu__item"
                    isDisabled={folderActionBusy || !canRemove}
                    textValue="Remove from Co-work"
                  >
                    Remove from Co-work
                  </MenuItem>
                ) : (
                  <MenuItem
                    id="discard-local"
                    className="wb-action-menu__item"
                    isDisabled={
                      onDiscardLocalDocument === undefined ||
                      folderActionBusy ||
                      syncStatus === "hydrating" ||
                      syncStatus === "saving" ||
                      syncStatus === "retrying"
                    }
                    textValue="Discard document"
                  >
                    Discard document
                  </MenuItem>
                )}
              </Menu>
            </Popover>
          </MenuTrigger>
        ) : null}
        {syncStatus === "clean" &&
        (materializationState?.kind === "conflict" ||
          materializationState?.kind === "error") ? (
          <span
            className={`wb-cowork__save-message is-${materializationState.kind}`}
            role="alert"
          >
            {coworkErrorMessage(
              materializationState.error,
              "Co-work couldn’t save the file.",
            )}
          </span>
        ) : null}
      </div>
    </header>
  );
}
