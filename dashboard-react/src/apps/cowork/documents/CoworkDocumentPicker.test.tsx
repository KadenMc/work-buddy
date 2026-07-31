import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CoworkDocumentSummary, CoworkScratchSummary } from "../contracts";
import { CoworkDocumentPicker } from "./CoworkDocumentPicker";

const document: CoworkDocumentSummary = {
  documentId: "doc-1",
  path: "notes/first-working-note.md",
  title: "First Working Note",
  profile: "co_authored",
  lifecycle: "active",
  initializationState: "ready",
  driftState: "clean",
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

const localDocument: CoworkScratchSummary = {
  scratchId: "scratch-1",
  title: "Untitled",
  createdAt: "2026-07-26T12:00:00Z",
  updatedAt: "2026-07-26T12:05:00Z",
  recoveredFromPreviousEditor: false,
};

const renderPicker = (onOpen = vi.fn(async () => undefined)) => {
  const onClose = vi.fn();
  render(
    <CoworkDocumentPicker
      documents={[document]}
      localDocuments={[localDocument]}
      folderName="research-notes"
      onClose={onClose}
      onOpen={onOpen}
      onOpenLocal={vi.fn()}
      onRepair={vi.fn()}
    />,
  );
  return { onClose, onOpen };
};

describe("CoworkDocumentPicker activation", () => {
  it("opens a ready document on an ordinary single pointer click", async () => {
    const user = userEvent.setup();
    const { onClose, onOpen } = renderPicker();

    await user.click(screen.getByRole("option", { name: /First Working Note/ }));

    await waitFor(() => expect(onOpen).toHaveBeenCalledTimes(1));
    expect(onOpen).toHaveBeenCalledWith(document);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("preserves keyboard activation through Enter", async () => {
    const user = userEvent.setup();
    const { onOpen } = renderPicker();
    const option = screen.getByRole("option", { name: /First Working Note/ });
    option.focus();

    await user.keyboard("{Enter}");

    await waitFor(() => expect(onOpen).toHaveBeenCalledTimes(1));
  });

  it("opens an on-device document from the same selector", async () => {
    const user = userEvent.setup();
    const onOpenLocal = vi.fn(async () => undefined);
    const onClose = vi.fn();
    render(
      <CoworkDocumentPicker
        documents={[]}
        localDocuments={[localDocument]}
        folderName={null}
        onClose={onClose}
        onOpen={vi.fn()}
        onOpenLocal={onOpenLocal}
        onRepair={vi.fn()}
      />,
    );

    const unsavedLabel = screen.getByText("Not saved to folder");
    expect(unsavedLabel.tagName).toBe("EM");
    expect(screen.getByText(/Saved in this browser/)).toBeVisible();
    expect(screen.getByPlaceholderText("Search by title")).toBeVisible();
    await user.click(
      screen.getByRole("option", {
        name: /Untitled.*Not saved to folder.*Saved in this browser/,
      }),
    );
    await waitFor(() => expect(onOpenLocal).toHaveBeenCalledWith(localDocument));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("contains document selection only, without duplicated creation actions", () => {
    renderPicker();

    expect(screen.queryByRole("button", { name: "From file" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Create new document" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Change Folder" })).toBeNull();
  });

  it("distinguishes an empty collection from an empty search result", async () => {
    const user = userEvent.setup();
    const shared = {
      localDocuments: [] as readonly CoworkScratchSummary[],
      folderName: "research-notes",
      onClose: vi.fn(),
      onOpen: vi.fn(),
      onOpenLocal: vi.fn(),
      onRepair: vi.fn(),
    };
    const { rerender } = render(
      <CoworkDocumentPicker {...shared} documents={[]} />,
    );

    expect(screen.getByText("No documents yet.")).toBeVisible();

    rerender(<CoworkDocumentPicker {...shared} documents={[document]} />);
    await user.type(screen.getByRole("textbox", { name: "Search documents" }), "missing");

    expect(screen.getByText("No documents match this search.")).toBeVisible();
  });

  it("explains when the only documents need attention", () => {
    render(
      <CoworkDocumentPicker
        documents={[
          {
            ...document,
            initializationState: "bootstrap_required",
            permissions: {
              open: false,
              edit: true,
              materialize: true,
              repair: true,
              retire: true,
            },
          },
        ]}
        localDocuments={[]}
        folderName="research-notes"
        onClose={vi.fn()}
        onOpen={vi.fn()}
        onOpenLocal={vi.fn()}
        onRepair={vi.fn()}
      />,
    );

    expect(screen.getByText("No documents are ready to open.")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Needs attention" })).toBeVisible();
    expect(screen.getByText("research-notes · notes/first-working-note.md")).toBeVisible();
  });

  it("names the folder for every folder-backed document", () => {
    renderPicker();

    expect(
      screen.getByPlaceholderText("Search by title or relative path"),
    ).toBeVisible();
    expect(
      screen.getByRole("option", {
        name: /First Working Note.*research-notes.*notes\/first-working-note\.md/,
      }),
    ).toBeVisible();
  });
});
