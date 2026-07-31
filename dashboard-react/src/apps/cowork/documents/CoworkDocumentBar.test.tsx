import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CoworkFolderSummary, CoworkViewModel } from "../contracts";
import { CoworkDocumentBar } from "./CoworkDocumentBar";

const document = {
  documentId: "doc-1",
  path: "notes/doc.md",
  title: "Document",
  profile: "co_authored",
  sourceWriteback: "same_file" as const,
  driftState: "clean" as const,
  openProposalCount: 0,
  openFlagCount: 0,
  permissions: {
    open: true,
    edit: true,
    materialize: true,
    repair: false,
    retire: true,
  },
};

const model: CoworkViewModel = {
  folders: [],
  folderChooser: {
    available: true,
    kind: "host",
    importAvailable: true,
    locationAvailable: true,
  },
  folderSelection: { kind: "none" },
  activeFolderStoreId: "store-1",
  catalog: { status: "ready", documents: [document], refreshedAt: null, error: null },
  scratches: [],
  routeTarget: { kind: "registered", storeId: "store-1", documentId: "doc-1" },
  activeSession: { kind: "registered", storeId: "store-1", document },
  openingTarget: null,
  navigationError: null,
  readOnly: false,
  document,
};

const folder: CoworkFolderSummary = {
  storeId: "store-1",
  folderName: "work-buddy",
  folderPath: "C:/Projects/work-buddy",
  layout: "wbuddy_cowork_v1",
  reachable: true,
  eligibility: "eligible",
  ineligibleReason: null,
  documentSurface: {
    enabled: true,
    allowedDocumentClasses: ["co_authored"],
    feedbackCapture: true,
  },
  permissions: {
    read: true,
    create: true,
    import: true,
    materialize: true,
    retire: true,
  },
  documentCount: 0,
};

const baseProps = {
  model,
  onChooseFolder: vi.fn(),
  onCloseFolder: vi.fn(),
  onOpenPicker: vi.fn(),
  onCreate: vi.fn(),
  onImportFile: vi.fn(),
  onCloseSession: vi.fn(),
  onPromoteScratch: vi.fn(),
};

describe("CoworkDocumentBar Save", () => {
  it("shows one concrete save state and invokes Save", async () => {
    const user = userEvent.setup();
    const onSaveMarkdown = vi.fn();
    render(
      <CoworkDocumentBar
        {...baseProps}
        syncStatus="clean"
        materializationState={{
          kind: "unsaved",
          fileSha256: "a".repeat(64),
        }}
        onSaveMarkdown={onSaveMarkdown}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Unsaved changes");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(onSaveMarkdown).toHaveBeenCalledTimes(1);
  });

  it("treats imported Markdown as a durable Co-work copy without source Save", () => {
    const importedDocument = {
      ...document,
      sourceWriteback: "never" as const,
      permissions: { ...document.permissions, materialize: false },
    };
    const importedModel: CoworkViewModel = {
      ...model,
      catalog: { ...model.catalog, documents: [importedDocument] },
      activeSession: {
        kind: "registered",
        storeId: "store-1",
        document: importedDocument,
      },
      document: importedDocument,
    };
    render(
      <CoworkDocumentBar
        {...baseProps}
        model={importedModel}
        syncStatus="clean"
        materializationState={{
          kind: "read_only",
          reason: "Source writeback is disabled.",
        }}
        onSaveMarkdown={vi.fn()}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Saved in Co-work");
    expect(screen.getByText("Import source: notes/doc.md")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("fails closed when source writeback is not explicitly allowed", () => {
    const documentWithoutPolicy = {
      ...document,
      sourceWriteback: undefined,
    };
    const unsafeModel: CoworkViewModel = {
      ...model,
      catalog: { ...model.catalog, documents: [documentWithoutPolicy] },
      activeSession: {
        kind: "registered",
        storeId: "store-1",
        document: documentWithoutPolicy,
      },
      document: documentWithoutPolicy,
    };

    render(
      <CoworkDocumentBar
        {...baseProps}
        model={unsafeModel}
        syncStatus="clean"
        materializationState={{
          kind: "unsaved",
          fileSha256: "a".repeat(64),
        }}
        onSaveMarkdown={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("keeps external-write conflicts visible and disables unsafe overwrite", () => {
    render(
      <CoworkDocumentBar
        {...baseProps}
        syncStatus="clean"
        materializationState={{
          kind: "conflict",
          fileSha256: "a".repeat(64),
          canRetry: false,
          error: {
            code: "stale_file",
            message: "Markdown changed outside Co-work; review it before saving.",
            retryable: false,
          },
        }}
        onSaveMarkdown={vi.fn()}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Markdown changed outside Co-work",
    );
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("prioritizes device-safe sync recovery over a second file-save retry", () => {
    render(
      <CoworkDocumentBar
        {...baseProps}
        syncStatus="offline"
        materializationState={{
          kind: "error",
          fileSha256: "a".repeat(64),
          canRetry: true,
          error: {
            code: "network_error",
            message: "The network connection was lost.",
            retryable: true,
          },
        }}
        onSaveMarkdown={vi.fn()}
        onRetrySync={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "Try saving again" })).toBeNull();
    expect(screen.getByRole("button", { name: "Sync now" })).toBeEnabled();
    expect(screen.getByRole("status")).toHaveTextContent("Saved in this browser");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("offers the file-save retry once Co-work sync is clean", () => {
    render(
      <CoworkDocumentBar
        {...baseProps}
        syncStatus="clean"
        materializationState={{
          kind: "error",
          fileSha256: "a".repeat(64),
          canRetry: true,
          error: {
            code: "network_error",
            message: "The network connection was lost.",
            retryable: true,
          },
        }}
        onSaveMarkdown={vi.fn()}
        onRetrySync={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Try saving again" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Sync now" })).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("network connection was lost");
  });

  it("blocks external replacement while local outbox edits are offline", async () => {
    const user = userEvent.setup();
    const onReviewExternalChanges = vi.fn();
    const driftedDocument = { ...document, driftState: "drifted" as const };
    const driftedModel: CoworkViewModel = {
      ...model,
      catalog: { ...model.catalog, documents: [driftedDocument] },
      activeSession: {
        kind: "registered",
        storeId: "store-1",
        document: driftedDocument,
      },
      document: driftedDocument,
    };
    render(
      <CoworkDocumentBar
        {...baseProps}
        model={driftedModel}
        syncStatus="offline"
        materializationState={{
          kind: "conflict",
          fileSha256: "a".repeat(64),
          canRetry: false,
          error: {
            code: "stale_file",
            message: "Markdown changed outside Co-work.",
            retryable: false,
          },
        }}
        onReviewExternalChanges={onReviewExternalChanges}
      />,
    );

    const review = screen.getByRole("button", { name: "Review file changes" });
    expect(review).toBeDisabled();
    expect(review).toHaveAccessibleDescription(
      "Sync this document to Co-work before reviewing external changes.",
    );
    expect(
      screen.getByText("Sync this document to Co-work before reviewing external changes."),
    ).toBeVisible();
    await user.click(review);
    expect(onReviewExternalChanges).not.toHaveBeenCalled();
  });

  it("does not offer document saving until the visible editor exposes an export handle", () => {
    const scratchModel: CoworkViewModel = {
      ...model,
      routeTarget: { kind: "scratch", scratchId: "scratch-1", title: "Draft" },
      activeSession: { kind: "scratch", scratchId: "scratch-1", title: "Draft" },
      document: null,
    };
    const props = {
      ...baseProps,
      model: scratchModel,
      onPromoteScratch: vi.fn(),
      onRetrySync: vi.fn(),
    };
    const { rerender } = render(
      <CoworkDocumentBar {...props} promotionReady={false} syncStatus="error" />,
    );

    expect(screen.getByRole("button", { name: "Loading editor…" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Save document" })).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Couldn’t save in this browser");
    expect(screen.queryByRole("button", { name: "Try saving again" })).toBeNull();

    rerender(<CoworkDocumentBar {...props} promotionReady syncStatus="clean" />);
    expect(screen.getByRole("button", { name: "Save document" })).toBeEnabled();
    expect(screen.getByRole("status")).toHaveTextContent("Saved in this browser");
  });

  it("keeps an on-device document local when the dashboard is read-only", async () => {
    const user = userEvent.setup();
    const onPromoteScratch = vi.fn();
    const scratchModel: CoworkViewModel = {
      ...model,
      readOnly: true,
      routeTarget: { kind: "scratch", scratchId: "scratch-1", title: "Draft" },
      activeSession: { kind: "scratch", scratchId: "scratch-1", title: "Draft" },
      document: null,
    };
    render(
      <CoworkDocumentBar
        {...baseProps}
        model={scratchModel}
        promotionReady
        syncStatus="clean"
        onPromoteScratch={onPromoteScratch}
      />,
    );

    const save = screen.getByRole("button", { name: "Save document" });
    expect(save).toBeDisabled();
    expect(save).toHaveAccessibleDescription(
      "Read-only mode. This document will stay in this browser.",
    );
    expect(
      screen.getByText("Read-only mode. This document will stay in this browser."),
    ).toBeVisible();
    await user.click(save);
    expect(onPromoteScratch).not.toHaveBeenCalled();
  });

  it("keeps an on-device document local when Folder selection is unavailable", () => {
    const scratchModel: CoworkViewModel = {
      ...model,
      folderChooser: {
        ...model.folderChooser,
        available: false,
      },
      routeTarget: { kind: "scratch", scratchId: "scratch-1", title: "Draft" },
      activeSession: { kind: "scratch", scratchId: "scratch-1", title: "Draft" },
      document: null,
    };
    render(
      <CoworkDocumentBar
        {...baseProps}
        model={scratchModel}
        promotionReady
        syncStatus="clean"
      />,
    );

    const save = screen.getByRole("button", { name: "Save document" });
    expect(save).toBeDisabled();
    expect(save).toHaveAccessibleDescription(
      "Choosing a folder isn’t available here. This document will stay in this browser.",
    );
    expect(
      screen.getByText(
        "Choosing a folder isn’t available here. This document will stay in this browser.",
      ),
    ).toBeVisible();
  });

  it("keeps an on-device document local when the active Folder denies create", () => {
    const blockedFolder: CoworkFolderSummary = {
      ...folder,
      permissions: { ...folder.permissions, create: false },
    };
    const scratchModel: CoworkViewModel = {
      ...model,
      folders: [blockedFolder],
      folderSelection: { kind: "initialized", folder: blockedFolder },
      routeTarget: { kind: "scratch", scratchId: "scratch-1", title: "Draft" },
      activeSession: { kind: "scratch", scratchId: "scratch-1", title: "Draft" },
      document: null,
    };
    render(
      <CoworkDocumentBar
        {...baseProps}
        model={scratchModel}
        promotionReady
        syncStatus="clean"
      />,
    );

    expect(screen.getByRole("button", { name: "Save document" })).toBeDisabled();
    expect(
      screen.getByText(
        "This folder doesn’t allow new documents. This document will stay in this browser.",
      ),
    ).toBeVisible();
  });

  it("puts the destructive on-device discard action behind the overflow menu", async () => {
    const user = userEvent.setup();
    const onDiscardLocalDocument = vi.fn();
    const scratchModel: CoworkViewModel = {
      ...model,
      routeTarget: { kind: "scratch", scratchId: "scratch-1", title: "Untitled" },
      activeSession: { kind: "scratch", scratchId: "scratch-1", title: "Untitled" },
      document: null,
    };
    render(
      <CoworkDocumentBar
        {...baseProps}
        model={scratchModel}
        syncStatus="clean"
        promotionReady
        onDiscardLocalDocument={onDiscardLocalDocument}
      />,
    );

    expect(screen.queryByRole("menuitem", { name: "Discard document" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "More document actions" }));
    await user.click(screen.getByRole("menuitem", { name: "Discard document" }));

    expect(onDiscardLocalDocument).toHaveBeenCalledTimes(1);
  });

  it("opens the native folder picker directly without a second menu action", async () => {
    const user = userEvent.setup();
    const onChooseFolder = vi.fn();
    render(<CoworkDocumentBar {...baseProps} onChooseFolder={onChooseFolder} />);

    await user.click(screen.getByRole("button", { name: "Open folder" }));

    expect(onChooseFolder).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.queryByText(/Open another Folder/i)).toBeNull();
  });

  it("keeps New and From file in the toolbar across Folder states", () => {
    const noFolderModel: CoworkViewModel = {
      ...model,
      activeFolderStoreId: null,
      routeTarget: { kind: "launcher", storeId: null },
      activeSession: { kind: "none" },
      catalog: { ...model.catalog, documents: [] },
      document: null,
    };
    const { rerender } = render(
      <CoworkDocumentBar {...baseProps} model={noFolderModel} />,
    );

    expect(screen.getByRole("button", { name: "New" })).toBeEnabled();
    const unavailableImport = screen.getByRole("button", {
      name: "From file",
    });
    expect(unavailableImport).toHaveAttribute("aria-disabled", "true");
    expect(unavailableImport).toHaveAccessibleDescription(
      "Open a folder before importing a file.",
    );
    unavailableImport.focus();
    expect(unavailableImport).toHaveFocus();
    expect(screen.queryByRole("button", { name: "Close folder" })).toBeNull();

    const folderModel: CoworkViewModel = {
      ...noFolderModel,
      folders: [folder],
      folderSelection: { kind: "initialized", folder },
      activeFolderStoreId: folder.storeId,
      routeTarget: { kind: "launcher", storeId: folder.storeId },
    };
    rerender(<CoworkDocumentBar {...baseProps} model={folderModel} />);

    expect(screen.getByRole("button", { name: "New" })).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "From file" }),
    ).toBeEnabled();
    expect(screen.getByRole("button", { name: "Close folder" })).toBeVisible();
  });

  it("closes the selected Folder through a distinct, discoverable control", async () => {
    const user = userEvent.setup();
    const onCloseFolder = vi.fn();
    const folderModel: CoworkViewModel = {
      ...model,
      folders: [folder],
      folderSelection: { kind: "initialized", folder },
    };
    const { rerender } = render(
      <CoworkDocumentBar
        {...baseProps}
        model={folderModel}
        onCloseFolder={onCloseFolder}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Close folder" }));
    expect(onCloseFolder).toHaveBeenCalledTimes(1);

    rerender(
      <CoworkDocumentBar
        {...baseProps}
        model={{
          ...folderModel,
          activeFolderStoreId: null,
          folderSelection: { kind: "none" },
          routeTarget: { kind: "launcher", storeId: null },
        }}
        onCloseFolder={onCloseFolder}
      />,
    );
    expect(screen.getByRole("button", { name: "Open folder" })).toHaveFocus();
  });

  it("shows and announces a pending Folder close", () => {
    render(
      <CoworkDocumentBar
        {...baseProps}
        model={{
          ...model,
          folders: [folder],
          folderSelection: { kind: "initialized", folder },
        }}
        folderActionBusy
        closingFolder
      />,
    );

    const closing = screen.getByRole("button", { name: "Closing…" });
    expect(closing).toBeDisabled();
    expect(closing.parentElement).toHaveAttribute(
      "aria-busy",
      "true",
    );
    expect(screen.getByText("Closing folder…", { exact: true })).toHaveAttribute(
      "role",
      "status",
    );
  });
});
