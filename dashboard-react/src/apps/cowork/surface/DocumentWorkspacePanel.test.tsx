import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CoworkBridge } from "../bridge";
import type { BoundDocumentRef, DocumentSession } from "../session/DocumentSession";
import { DocumentWorkspacePanel } from "./DocumentWorkspacePanel";

vi.mock("./DocumentEditorSurface", () => ({
  DocumentEditorSurface: () => <div data-testid="live-document-editor">Editor</div>,
}));

const reference: BoundDocumentRef = {
  kind: "domain-bound",
  storeId: "store-1",
  documentId: "doc-1",
  binding: {
    bindingId: "binding-1",
    domain: {
      namespace: "tasks",
      kind: "task",
      entityId: "task-1",
      role: "note",
    },
    authorityEpoch: 1,
    projectionMode: "none",
  },
};

const session: DocumentSession = {
  key: JSON.stringify(["store-1", "doc-1"]),
  reference: { kind: "workspace", storeId: "store-1", documentId: "doc-1" },
  bridge: {} as CoworkBridge,
  writable: true,
  syncStatus: "saved_on_device",
};

describe("DocumentWorkspacePanel", () => {
  it("keeps domain context beside the live editor and exposes explicit presentation actions", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onOpenFull = vi.fn();
    render(
      <DocumentWorkspacePanel
        reference={reference}
        session={session}
        title="Task note"
        primary={<main>Task details</main>}
        canOpenFull
        onClose={onClose}
        onOpenFull={onOpenFull}
      />,
    );

    expect(screen.getByText("Task details")).toBeInTheDocument();
    expect(screen.getByTestId("live-document-editor")).toBeInTheDocument();
    expect(screen.getByText("Saved on this device")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Task note" })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Open full" }));
    expect(onOpenFull).toHaveBeenCalledWith(reference);
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("rejects a panel/session identity mismatch", () => {
    expect(() => render(
      <DocumentWorkspacePanel
        reference={{ ...reference, documentId: "other" }}
        session={session}
        title="Task note"
        primary={<div />}
        canOpenFull
        onClose={() => undefined}
        onOpenFull={() => undefined}
      />,
    )).toThrow(/does not match/i);
  });
});
