import { DotsThree } from "@phosphor-icons/react/DotsThree";
import { FolderSimple } from "@phosphor-icons/react/FolderSimple";
import { NotePencil } from "@phosphor-icons/react/NotePencil";
import { X } from "@phosphor-icons/react/X";
import { Menu, MenuItem, MenuTrigger, Popover, type Key } from "react-aria-components";

import { Button } from "../../../ui";
import type { CoworkFolderSummary, CoworkViewModel } from "../contracts";
import type { CoworkSyncStatus } from "../persistence/CoworkYdocPersistence";
import type { CoworkMaterializationState } from "../materialization/contracts";
import { coworkErrorMessage } from "../providers/errors";

interface CoworkDocumentBarProps {
  readonly model: CoworkViewModel;
  readonly onChooseFolder: () => void;
  readonly folderActionBusy?: boolean;
  readonly onOpenPicker: () => void;
  readonly onCreate: () => void;
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
    return "Read-only mode. This document will stay on this device.";
  }
  if (folder === null && !model.folderChooser.available) {
    return "Folder selection isn’t available here. This document will stay on this device.";
  }
  if (folder !== null && !folder.permissions.create) {
    return "This Folder doesn’t allow new documents. This document will stay on this device.";
  }
  return null;
};

const registeredStatusLabel = (
  model: CoworkViewModel,
  syncStatus?: CoworkSyncStatus,
  state?: CoworkMaterializationState,
): string => {
  const sync = syncStatus ?? (model.readOnly ? "read_only" : "hydrating");
  if (sync === "read_only" || state?.kind === "read_only") return "Read-only";
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
  if (sync === "saved_on_device" || sync === "offline") return "Saved on this device";
  if (sync === "conflict") return "Sync conflict";
  if (sync === "error") return "Couldn’t save";
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
  if (sync === "offline" || sync === "error") return "Couldn’t save on this device";
  if (sync === "conflict") return "Save conflict";
  if (sync === "read_only") return "Read-only";
  return "Saved on this device";
};

export function CoworkDocumentBar({
  model,
  onChooseFolder,
  folderActionBusy = false,
  onOpenPicker,
  onCreate,
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
    document?.driftState === "clean" &&
    syncStatus === "clean" &&
    materializationState?.kind === "up_to_date";

  return (
    <header className="wb-cowork__document-bar" aria-label="Co-work document controls">
      <div className="wb-cowork__document-context">
        <Button
          variant="ghost"
          className="wb-cowork__folder-trigger"
          onClick={onChooseFolder}
          title={folder?.folderPath ?? "Open Folder"}
          disabled={!model.folderChooser.available || folderControlBusy}
        >
          <FolderSimple weight="duotone" aria-hidden="true" />
          <span>{openingFolder ? "Opening…" : folder?.folderName ?? "Open Folder"}</span>
        </Button>

        <Button
          variant="ghost"
          className="wb-cowork__document-trigger"
          onClick={onOpenPicker}
          disabled={folder === null || folderControlBusy}
          title={
            document?.path ??
            (scratch === null ? "Open document" : "Saved on this device")
          }
        >
          <span>{document?.title ?? scratch?.title ?? "Open document"}</span>
          {document !== null ? (
            <small>{document.path}</small>
          ) : null}
        </Button>
      </div>

      <div className="wb-cowork__document-actions">
        <span className="wb-cowork__sync-status" role="status" aria-live="polite">
          {model.openingTarget !== null
            ? "Loading document…"
            : document !== null
              ? registeredStatusLabel(model, syncStatus, materializationState)
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
        ) : document !== null && document.permissions?.materialize !== false ? (
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
        ) : folder !== null && document === null ? (
          <Button
            size="small"
            variant="primary"
            onClick={onCreate}
            disabled={folderActionBusy || !folder.permissions.create}
          >
            <NotePencil aria-hidden="true" /> New
          </Button>
        ) : folder !== null && document !== null ? (
          <Button
            size="small"
            onClick={onCreate}
            disabled={folderActionBusy || !folder.permissions.create}
          >
            <NotePencil aria-hidden="true" /> New
          </Button>
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
            <X aria-hidden="true" /> Close
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
