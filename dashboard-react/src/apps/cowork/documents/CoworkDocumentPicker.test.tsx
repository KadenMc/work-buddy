import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CoworkDocumentSummary, CoworkFolderSummary } from "../contracts";
import { CoworkDocumentPicker } from "./CoworkDocumentPicker";

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
  documentCount: 1,
};

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

const renderPicker = (onOpen = vi.fn(async () => undefined)) => {
  const onClose = vi.fn();
  render(
    <CoworkDocumentPicker
      folder={folder}
      documents={[document]}
      onClose={onClose}
      onOpen={onOpen}
      onCreate={vi.fn()}
      onRegister={vi.fn()}
      onRepair={vi.fn()}
      onChangeFolder={vi.fn()}
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
});
