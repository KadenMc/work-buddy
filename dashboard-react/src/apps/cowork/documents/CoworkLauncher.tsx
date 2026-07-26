import { FolderOpen } from "@phosphor-icons/react/FolderOpen";
import { NotePencil } from "@phosphor-icons/react/NotePencil";
import { useState } from "react";
import { Input, Label, TextField } from "react-aria-components";

import { Button, InlineAlert, Spinner } from "../../../ui";
import type {
  CoworkDocumentSummary,
  CoworkFolderSummary,
  CoworkScratchSummary,
  CoworkViewModel,
} from "../contracts";

interface CoworkLauncherProps {
  readonly model: CoworkViewModel;
  readonly onChooseFolder: () => void;
  readonly onInspectPath: (folderPath: string) => void;
  readonly onContinueInspection: () => void;
  readonly onRetryInspection: () => void;
  readonly onCancelInspection: () => void;
  readonly onInitialize: () => void;
  readonly onOpenFolder: (storeId: string) => void;
  readonly onOpenPicker: () => void;
  readonly onOpenDocument: (document: CoworkDocumentSummary) => void;
  readonly onCreate: () => void;
  readonly onRegister: () => void;
  readonly onOpenScratch: (scratch: CoworkScratchSummary) => void;
  readonly onNewScratch: () => void;
}

const unavailableCopy: Record<string, string> = {
  folder_not_found: "That Folder no longer exists on the Work Buddy machine.",
  folder_unreadable: "Work Buddy cannot read that Folder.",
  folder_disallowed: "That Folder is outside the host paths Work Buddy may use.",
  descendant_scan_incomplete:
    "Co-work could not yet prove that setup would avoid a nested Folder.",
  folder_too_large_for_safe_setup:
    "This Folder is too large to verify safely with current settings. Choose a narrower Folder.",
};

const folderConflictCopy: Record<string, string> = {
  folder_layout_incomplete:
    "This Folder has a partial Co-work setup that cannot be completed safely.",
  folder_store_collision:
    "This Folder’s Co-work identity is already registered to another location.",
  identity_conflict:
    "This Folder’s Co-work identity does not match its registered location.",
};

const folderFor = (model: CoworkViewModel): CoworkFolderSummary | null =>
  model.folderSelection.kind === "initialized"
    ? model.folderSelection.folder
    : model.folders.find((entry) => entry.storeId === model.activeFolderStoreId) ?? null;

export function CoworkLauncher({
  model,
  onChooseFolder,
  onInspectPath,
  onContinueInspection,
  onRetryInspection,
  onCancelInspection,
  onInitialize,
  onOpenFolder,
  onOpenPicker,
  onOpenDocument,
  onCreate,
  onRegister,
  onOpenScratch,
  onNewScratch,
}: CoworkLauncherProps) {
  const [manualPath, setManualPath] = useState("");
  const selection = model.folderSelection;
  const folder = folderFor(model);

  if (selection.kind === "choosing" || selection.kind === "inspecting") {
    return (
      <section className="wb-cowork-launcher wb-cowork-launcher--centered" aria-busy="true">
        <Spinner />
        <h2>{selection.kind === "choosing" ? "Opening Folder…" : "Checking for Co-work…"}</h2>
        <p>Co-work is inspecting the Folder without changing it.</p>
        <Button onClick={onCancelInspection}>Cancel</Button>
      </section>
    );
  }

  if (selection.kind === "inspecting_descendants") {
    return (
      <section className="wb-cowork-launcher wb-cowork-launcher--centered">
        <Spinner />
        <h2>Checking Folder boundaries…</h2>
        <p>{selection.progress.visited.toLocaleString()} locations checked. Nothing has been written.</p>
        <div className="wb-cowork-launcher__actions">
          <Button onClick={onCancelInspection}>Cancel</Button>
          <Button variant="primary" onClick={onContinueInspection}>Continue check</Button>
        </div>
      </section>
    );
  }

  if (selection.kind === "setup_available") {
    return (
      <section className="wb-cowork-launcher wb-cowork-launcher--centered">
        <FolderOpen weight="duotone" aria-hidden="true" className="wb-cowork-launcher__hero-icon" />
        <h2>Set up Co-work in “{selection.candidate.folderName}”?</h2>
        <p>
          Co-work will add a tool-managed <code>.wbuddy</code> directory. Its document history,
          review state, provenance, and recovery data live under <code>.wbuddy/cowork/</code>.
        </p>
        <p className="wb-cowork-launcher__path" title={selection.candidate.folderPath}>
          {selection.candidate.folderPath}
        </p>
        <div className="wb-cowork-launcher__actions">
          <Button onClick={onCancelInspection}>Cancel</Button>
          <Button variant="primary" onClick={onInitialize}>Set up Co-work</Button>
        </div>
      </section>
    );
  }

  if (selection.kind === "inside_existing_folder") {
    return (
      <section className="wb-cowork-launcher wb-cowork-launcher--centered">
        <InlineAlert tone="warning">
          This location is already inside the Co-work Folder “{selection.owner.folderName}”.
        </InlineAlert>
        <div className="wb-cowork-launcher__actions">
          <Button onClick={onCancelInspection}>Choose another Folder</Button>
          <Button variant="primary" onClick={() => onOpenFolder(selection.owner.storeId)}>
            Open {selection.owner.folderName}
          </Button>
        </div>
      </section>
    );
  }

  if (selection.kind === "contains_nested_folder") {
    return (
      <section className="wb-cowork-launcher wb-cowork-launcher--centered">
        <InlineAlert tone="warning">
          This Folder already contains another Folder with Co-work set up, so Co-work will
          not set it up again here.
        </InlineAlert>
        <ul className="wb-cowork-launcher__boundaries">
          {selection.boundaries.map((boundary) => (
            <li key={boundary.folderPath}>{boundary.folderName} · {boundary.folderPath}</li>
          ))}
        </ul>
        <Button onClick={onChooseFolder}>Choose another Folder</Button>
      </section>
    );
  }

  if (selection.kind === "store_layout_conflict") {
    return (
      <section className="wb-cowork-launcher wb-cowork-launcher--centered">
        <InlineAlert tone="danger">
          {folderConflictCopy[selection.reasonCode] ??
            "Co-work found Folder data it cannot reconcile safely."}{" "}
          It did not replace or merge anything.
        </InlineAlert>
        <p className="wb-cowork-launcher__path">{selection.candidate.folderPath}</p>
        <div className="wb-cowork-launcher__actions">
          {selection.availableActions.includes("retry") ? <Button onClick={onRetryInspection}>Retry inspection</Button> : null}
          <Button onClick={onChooseFolder}>Choose another Folder</Button>
        </div>
      </section>
    );
  }

  if (selection.kind === "unavailable") {
    return (
      <section className="wb-cowork-launcher wb-cowork-launcher--centered">
        <InlineAlert tone="danger">
          {unavailableCopy[selection.reasonCode] ?? "This Folder is unavailable to Co-work."}
        </InlineAlert>
        {selection.candidate !== null ? <p className="wb-cowork-launcher__path">{selection.candidate.folderPath}</p> : null}
        <div className="wb-cowork-launcher__actions">
          {selection.retryable ? <Button onClick={onRetryInspection}>Retry check</Button> : null}
          <Button onClick={onChooseFolder}>Choose another Folder</Button>
        </div>
      </section>
    );
  }

  if (folder !== null) {
    const readyDocuments = model.catalog.documents
      .filter((document) => (document.initializationState ?? "ready") === "ready")
      .slice(0, 6);
    return (
      <section className="wb-cowork-launcher">
        <div className="wb-cowork-launcher__hero">
          <NotePencil weight="duotone" aria-hidden="true" className="wb-cowork-launcher__hero-icon" />
          <h2>Start a Co-work document in “{folder.folderName}”</h2>
          <p>Write with clean Markdown output and review agent suggestions beside the document.</p>
          <div className="wb-cowork-launcher__actions">
            {readyDocuments.length > 0 ? <Button onClick={onOpenPicker}>Open existing</Button> : null}
            <Button variant="primary" onClick={onCreate} disabled={!folder.permissions.create}>Create new document</Button>
            <Button variant="ghost" onClick={onRegister} disabled={!folder.permissions.import}>Register existing Markdown</Button>
          </div>
        </div>
        {model.catalog.status === "loading" ? <p role="status"><Spinner /> Loading documents…</p> : null}
        {model.catalog.error !== null ? (
          <InlineAlert tone="danger">
            {model.catalog.error.message} <Button size="small" onClick={onRetryInspection}>Retry</Button>
          </InlineAlert>
        ) : null}
        {readyDocuments.length > 0 ? (
          <section aria-labelledby="cowork-recent-documents" className="wb-cowork-launcher__section">
            <h3 id="cowork-recent-documents">Recent documents</h3>
            <div className="wb-cowork-launcher__cards">
              {readyDocuments.map((document) => (
                <button key={document.documentId} type="button" onClick={() => onOpenDocument(document)} className="wb-cowork-launcher__card">
                  <strong>{document.title}</strong>
                  <span>{document.path}</span>
                  {document.openProposalCount > 0 ? <small>{document.openProposalCount} open proposals</small> : null}
                </button>
              ))}
            </div>
          </section>
        ) : null}
        {model.scratches.length > 0 ? (
          <section aria-labelledby="cowork-device-scratches" className="wb-cowork-launcher__section">
            <h3 id="cowork-device-scratches">On this device</h3>
            {model.scratches.map((scratch) => (
              <div key={scratch.scratchId} className="wb-cowork-launcher__scratch">
                <span><strong>{scratch.title}</strong><small>Saved on this device</small></span>
                <Button size="small" onClick={() => onOpenScratch(scratch)}>Continue</Button>
              </div>
            ))}
          </section>
        ) : null}
      </section>
    );
  }

  return (
    <section className="wb-cowork-launcher wb-cowork-launcher--centered">
      <FolderOpen weight="duotone" aria-hidden="true" className="wb-cowork-launcher__hero-icon" />
      <h2>Choose a Folder for Co-work</h2>
      <p>A Folder keeps related documents together and defines where Markdown is saved.</p>
      {model.navigationError !== null ? <InlineAlert tone="danger">{model.navigationError.message}</InlineAlert> : null}
      <div className="wb-cowork-launcher__actions">
        <Button
          variant="primary"
          onClick={onChooseFolder}
          disabled={!model.folderChooser.available}
          title={
            model.folderChooser.available
              ? "Choose a Folder on the Work Buddy machine"
              : "A native host Folder chooser is not available; enter a host path below."
          }
        >
          Open Folder
        </Button>
        <Button onClick={onNewScratch}>New local scratch</Button>
      </div>
      {!model.folderChooser.available ? (
        <p className="wb-cowork-launcher__chooser-note">
          This dashboard cannot open the host’s native Folder chooser. Enter a Folder path on
          the Work Buddy machine below.
        </p>
      ) : null}
      <div className="wb-cowork-launcher__manual">
        <TextField value={manualPath} onChange={setManualPath} className="wb-cowork-field">
          <Label>Or enter a Folder path on the Work Buddy machine</Label>
          <Input placeholder="C:\\Projects\\my-folder" />
        </TextField>
        <Button onClick={() => onInspectPath(manualPath)} disabled={manualPath.trim().length === 0}>Inspect Folder</Button>
      </div>
      {model.folders.length > 0 ? (
        <section aria-labelledby="cowork-known-folders" className="wb-cowork-launcher__section">
          <h3 id="cowork-known-folders">Recent Folders</h3>
          <div className="wb-cowork-launcher__cards">
            {model.folders.map((known) => (
              <button key={known.storeId} type="button" onClick={() => onOpenFolder(known.storeId)} className="wb-cowork-launcher__card">
                <strong>{known.folderName}</strong><span>{known.folderPath}</span>
              </button>
            ))}
          </div>
        </section>
      ) : null}
      {model.scratches.length > 0 ? (
        <section aria-labelledby="cowork-device-scratches-empty" className="wb-cowork-launcher__section">
          <h3 id="cowork-device-scratches-empty">On this device</h3>
          {model.scratches.map((scratch) => (
            <div key={scratch.scratchId} className="wb-cowork-launcher__scratch">
              <span><strong>{scratch.title}</strong><small>{scratch.recoveredFromPreviousEditor ? "Recovered from the previous Co-work editor" : "Local scratch"}</small></span>
              <Button size="small" onClick={() => onOpenScratch(scratch)}>Continue</Button>
            </div>
          ))}
        </section>
      ) : null}
    </section>
  );
}
