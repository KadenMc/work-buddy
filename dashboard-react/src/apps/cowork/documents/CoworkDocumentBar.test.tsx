import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CoworkViewModel } from "../contracts";
import { CoworkDocumentBar } from "./CoworkDocumentBar";

const document = {
  documentId: "doc-1",
  path: "notes/doc.md",
  title: "Document",
  profile: "co_authored",
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
  folderChooser: { available: true, kind: "host" },
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

const baseProps = {
  model,
  onChooseFolder: vi.fn(),
  onOpenFolder: vi.fn(),
  onOpenPicker: vi.fn(),
  onCreate: vi.fn(),
  onCloseSession: vi.fn(),
  onPromoteScratch: vi.fn(),
};

describe("CoworkDocumentBar Save", () => {
  it("shows factual structured/projection state and invokes Save Markdown", async () => {
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

    expect(
      screen.getByText("Synced to Co-work · Markdown has unsaved changes"),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Save Markdown" }));
    expect(onSaveMarkdown).toHaveBeenCalledTimes(1);
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
    expect(screen.getByRole("button", { name: "Save Markdown" })).toBeDisabled();
  });

  it("offers an explicit retry for retryable Save failures", () => {
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
      />,
    );

    expect(screen.getByRole("button", { name: "Retry Save Markdown" })).toBeEnabled();
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

    const review = screen.getByRole("button", { name: "Review external changes" });
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

  it("does not offer scratch promotion until the visible editor exposes an export handle", () => {
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
    expect(screen.queryByRole("button", { name: "Save as document" })).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Device save failed");
    expect(screen.getByRole("button", { name: "Retry device save" })).toBeVisible();

    rerender(<CoworkDocumentBar {...props} promotionReady syncStatus="clean" />);
    expect(screen.getByRole("button", { name: "Save as document" })).toBeEnabled();
    expect(screen.getByRole("status")).toHaveTextContent("Saved on this device");
  });
});
