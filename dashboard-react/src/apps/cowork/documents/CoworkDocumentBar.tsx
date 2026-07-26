import { CaretDown } from "@phosphor-icons/react/CaretDown";
import { FolderSimple } from "@phosphor-icons/react/FolderSimple";
import { Plus } from "@phosphor-icons/react/Plus";
import { X } from "@phosphor-icons/react/X";
import { Menu, MenuItem, MenuTrigger, Popover, type Key } from "react-aria-components";

import { Button } from "../../../ui";
import type { CoworkViewModel } from "../contracts";
import type { CoworkSyncStatus } from "../persistence/CoworkYdocPersistence";
import type { CoworkMaterializationState } from "../materialization/contracts";

interface CoworkDocumentBarProps {
  readonly model: CoworkViewModel;
  readonly onChooseFolder: () => void;
  readonly onOpenFolder: (storeId: string) => void;
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

const projectionLabel = (
  model: CoworkViewModel,
  state?: CoworkMaterializationState,
): string | null => {
  if (model.activeSession.kind !== "registered") return null;
  if (state !== undefined) {
    if (state.kind === "checking") return "Checking Markdown…";
    if (state.kind === "up_to_date") return "Markdown up to date";
    if (state.kind === "unsaved") return "Markdown has unsaved changes";
    if (state.kind === "saving") return "Saving Markdown…";
    if (state.kind === "conflict") return "Markdown save conflict";
    if (state.kind === "error") return "Markdown save failed";
    return "Markdown is read-only";
  }
  const drift = model.activeSession.document.driftState;
  if (drift === "drifted") return "Markdown changed outside Co-work";
  if (drift === "missing") return "Markdown file missing";
  return "Markdown up to date";
};

export function CoworkDocumentBar({
  model,
  onChooseFolder,
  onOpenFolder,
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
}: CoworkDocumentBarProps) {
  const folder =
    model.folderSelection.kind === "initialized"
      ? model.folderSelection.folder
      : model.folders.find((entry) => entry.storeId === model.activeFolderStoreId) ?? null;
  const document = model.activeSession.kind === "registered"
    ? model.activeSession.document
    : null;
  const scratch = model.activeSession.kind === "scratch" ? model.activeSession : null;
  const folderItems = new Map(model.folders.map((entry) => [entry.storeId, entry] as const));
  const structuredLabel: Record<CoworkSyncStatus, string> = {
    hydrating: "Loading document…",
    clean: "Synced to Co-work",
    saving: "Saving to Co-work…",
    saved_on_device: "Saved on this device; waiting to sync",
    retrying: "Saving to Co-work…",
    offline: "Offline; edits are saved on this device",
    conflict: "Sync conflict",
    error: "Co-work sync failed",
    read_only: "Read-only",
  };
  const scratchLabel: Record<CoworkSyncStatus, string> = {
    hydrating: "Loading scratch…",
    clean: "Saved on this device",
    saving: "Saving on this device…",
    saved_on_device: "Saved on this device",
    retrying: "Saving on this device…",
    offline: "Device save unavailable",
    conflict: "Device save conflict",
    error: "Device save failed",
    read_only: "Read-only",
  };
  const reimportBlockedReason = coworkReimportLocalBlockedReason(
    syncStatus,
    materializationState,
  );

  return (
    <header className="wb-cowork__document-bar" aria-label="Co-work document controls">
      <div className="wb-cowork__document-context">
        <MenuTrigger>
          <Button
            variant="ghost"
            className="wb-cowork__folder-trigger"
            title={
              folder?.folderPath ??
              (model.folderChooser.available
                ? "Choose a Folder on the Work Buddy machine"
                : "Use the host Folder path field in the launcher")
            }
            disabled={folder === null && !model.folderChooser.available}
          >
            <FolderSimple weight="duotone" aria-hidden="true" />
            <span>{folder?.folderName ?? "Open Folder…"}</span>
            <CaretDown aria-hidden="true" />
          </Button>
          <Popover className="wb-popover wb-cowork__folder-popover" placement="bottom start">
            <Menu
              aria-label="Co-work Folders"
              className="wb-action-menu"
              onAction={(key: Key) => {
                if (String(key) === "choose") onChooseFolder();
                else if (folderItems.has(String(key))) onOpenFolder(String(key));
              }}
            >
              {model.folders.map((entry) => (
                <MenuItem
                  id={entry.storeId}
                  key={entry.storeId}
                  textValue={`${entry.folderName} ${entry.folderPath}`}
                  className="wb-action-menu__item wb-cowork__folder-item"
                >
                  <span><strong>{entry.folderName}</strong><small>{entry.folderPath}</small></span>
                </MenuItem>
              ))}
              <MenuItem
                id="choose"
                className="wb-action-menu__item"
                isDisabled={!model.folderChooser.available}
              >
                Open another Folder…
              </MenuItem>
            </Menu>
          </Popover>
        </MenuTrigger>

        <Button
          variant="ghost"
          className="wb-cowork__document-trigger"
          onClick={onOpenPicker}
          disabled={folder === null}
          title={document?.path ?? (scratch === null ? "Choose a document" : "Local scratch")}
        >
          <span>{document?.title ?? scratch?.title ?? "Open a document…"}</span>
          {document !== null ? <small>{document.path}</small> : scratch !== null ? <small>Local</small> : null}
          {folder !== null ? <CaretDown aria-hidden="true" /> : null}
        </Button>
      </div>

      <div className="wb-cowork__document-actions">
        <span className="wb-cowork__sync-status" role="status" aria-live="polite">
          {model.openingTarget !== null
            ? "Loading document…"
            : document !== null
              ? `${structuredLabel[syncStatus ?? (model.readOnly ? "read_only" : "hydrating")]} · ${projectionLabel(model, materializationState)}`
              : scratch !== null
                ? scratchLabel[syncStatus ?? "hydrating"]
                : ""}
        </span>
        {document?.driftState === "drifted" ? (
          <Button
            size="small"
            onClick={onReviewExternalChanges}
            disabled={
              onReviewExternalChanges === undefined || reimportBlockedReason !== null
            }
            aria-describedby={
              reimportBlockedReason === null ? undefined : "cowork-reimport-blocked-reason"
            }
            title={reimportBlockedReason ?? "Compare and safely replace from Markdown."}
          >
            Review external changes
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
          <Button
            size="small"
            variant="primary"
            onClick={onPromoteScratch}
            disabled={promotionBusy || !promotionReady}
            title={
              promotionReady
                ? "Create a registered Co-work document from this scratch."
                : "The scratch editor is still loading."
            }
          >
            {promotionBusy
              ? "Preparing…"
              : promotionReady
                ? "Save as document"
                : "Loading editor…"}
          </Button>
        ) : document !== null && document.permissions?.materialize !== false ? (
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
              ? "Saving Markdown…"
              : (materializationState?.kind === "conflict" ||
                    materializationState?.kind === "error") &&
                  materializationState.canRetry
                ? "Retry Save Markdown"
                : "Save Markdown"}
          </Button>
        ) : folder !== null && document === null ? (
          <Button size="small" variant="primary" onClick={onCreate} disabled={!folder.permissions.create}>
            <Plus aria-hidden="true" /> New
          </Button>
        ) : folder !== null && document !== null ? (
          <Button size="small" onClick={onCreate} disabled={!folder.permissions.create}>
            <Plus aria-hidden="true" /> New
          </Button>
        ) : null}
        {(document !== null || scratch !== null) &&
        (syncStatus === "offline" ||
          syncStatus === "error" ||
          syncStatus === "conflict") ? (
          <Button size="small" onClick={onRetrySync} disabled={onRetrySync === undefined}>
            {scratch !== null ? "Retry device save" : "Retry Co-work sync"}
          </Button>
        ) : null}
        {document !== null || scratch !== null ? (
          <Button size="small" variant="ghost" onClick={onCloseSession} aria-label="Close document">
            <X aria-hidden="true" /> Close
          </Button>
        ) : null}
        {document !== null ? (
          <Button
            size="small"
            variant="ghost"
            onClick={onRemoveDocument}
            disabled={
              onRemoveDocument === undefined ||
              document.permissions?.retire === false ||
              document.driftState !== "clean" ||
              syncStatus !== "clean" ||
              materializationState?.kind !== "up_to_date"
            }
            title={
              document.driftState !== "clean"
                ? "Resolve the external Markdown change before removing this document."
                : syncStatus !== "clean" || materializationState?.kind !== "up_to_date"
                  ? "Save and sync the document before removing it from Co-work."
                  : "Keep the Markdown file and history, but stop managing it in Co-work."
            }
          >
            Remove from Co-work
          </Button>
        ) : null}
        {materializationState?.kind === "conflict" ||
        materializationState?.kind === "error" ? (
          <span
            className={`wb-cowork__save-message is-${materializationState.kind}`}
            role="alert"
            title={materializationState.error.code}
          >
            {materializationState.error.message}
          </span>
        ) : null}
      </div>
    </header>
  );
}
