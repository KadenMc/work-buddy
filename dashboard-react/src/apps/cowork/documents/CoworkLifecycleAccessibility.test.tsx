import { useState, type ReactNode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import type { CoworkDocumentSummary, CoworkFolderSummary } from "../contracts";
import { CoworkHttpClient } from "../providers/CoworkHttpClient";
import { CoworkDocumentLifecycleDialog } from "./CoworkDocumentLifecycleDialog";
import { CoworkDocumentPicker } from "./CoworkDocumentPicker";
import { CoworkReimportDialog } from "./CoworkReimportDialog";
import { CoworkRetirementDialog } from "./CoworkRetirementDialog";

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
  path: "notes/working-note.md",
  title: "Working note",
  profile: "co_authored",
  lifecycle: "active",
  initializationState: "ready",
  structuredHeadSha256: "1".repeat(64),
  snapshotSha256: "2".repeat(64),
  projectionSha256: "3".repeat(64),
  currentFileSha256: "4".repeat(64),
  driftState: "drifted",
  openProposalCount: 0,
  openFlagCount: 0,
  permissions: {
    open: true,
    edit: true,
    materialize: true,
    repair: true,
    retire: true,
  },
};

const json = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });

function TriggerHarness({
  label,
  dialog,
}: {
  readonly label: string;
  readonly dialog: (close: () => void) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>{label}</button>
      {open ? dialog(() => setOpen(false)) : null}
    </>
  );
}

describe("Co-work lifecycle dialog accessibility and focus", () => {
  it("gives the picker search initial focus, has no axe violations, and restores its trigger", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <TriggerHarness
        label="Open picker"
        dialog={(close) => (
          <CoworkDocumentPicker
            folder={folder}
            documents={[document]}
            onClose={close}
            onOpen={vi.fn()}
            onCreate={vi.fn()}
            onRegister={vi.fn()}
            onRepair={vi.fn()}
            onChangeFolder={vi.fn()}
          />
        )}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Open picker" });
    await user.click(trigger);

    expect(await screen.findByRole("textbox", { name: "Search documents" })).toHaveFocus();
    await expectNoAccessibilityViolations(container);
    await user.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(trigger).toHaveFocus());
  }, 15_000);

  it("opens the focused picker option with one keyboard activation", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn(async () => undefined);
    const secondDocument: CoworkDocumentSummary = {
      ...document,
      documentId: "doc-2",
      title: "Second working note",
      path: "notes/second-working-note.md",
    };
    render(
      <CoworkDocumentPicker
        folder={folder}
        documents={[document, secondDocument]}
        currentDocumentId={document.documentId}
        onClose={vi.fn()}
        onOpen={onOpen}
        onCreate={vi.fn()}
        onRegister={vi.fn()}
        onRepair={vi.fn()}
        onChangeFolder={vi.fn()}
      />,
    );

    const option = screen.getByRole("option", {
      name: /Second working note.*second-working-note\.md/,
    });
    option.focus();
    await user.keyboard("{Enter}");

    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(secondDocument));
  });

  it.each([
    ["create", "New document", "Title"],
    ["register", "New document from Markdown", null],
    ["repair", "Repair document", null],
  ] as const)(
    "keeps the %s dialog accessible and restores focus",
    async (mode, heading, autofocusName) => {
      const user = userEvent.setup();
      const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
        if (String(input) === "/api/truth/cowork/files/choose-markdown") {
          return json(
            {
              error: {
                code: "folder_chooser_failed",
                message: "The picker could not start.",
                retryable: true,
              },
            },
            503,
          );
        }
        throw new Error(`Unexpected request: ${String(input)}`);
      });
      const { container } = render(
        <TriggerHarness
          label={`Open ${mode}`}
          dialog={(close) => (
            <CoworkDocumentLifecycleDialog
              mode={mode}
              folder={folder}
              client={new CoworkHttpClient(fetchImpl as typeof fetch)}
              repairDocument={mode === "repair" ? document : undefined}
              onClose={close}
              onOpened={vi.fn()}
            />
          )}
        />,
      );
      const trigger = screen.getByRole("button", { name: `Open ${mode}` });
      await user.click(trigger);
      const dialog = await screen.findByRole("dialog", { name: heading });
      if (autofocusName !== null) {
        expect(screen.getByRole("textbox", { name: autofocusName })).toHaveFocus();
      } else {
        expect(dialog).toContainElement(globalThis.document.activeElement as HTMLElement);
      }
      if (mode === "register") {
        await screen.findByRole("button", { name: "Choose again" });
      }
      await expectNoAccessibilityViolations(container);
      await user.click(screen.getByRole("button", { name: "Cancel" }));
      await waitFor(() => expect(trigger).toHaveFocus());
    },
    15_000,
  );

  it("keeps re-import review accessible, contained, and focus-restoring", async () => {
    const user = userEvent.setup();
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/drift?")) {
        return json({
          state: "drifted",
          last_materialized_sha256: document.projectionSha256,
          current_file_sha256: document.currentFileSha256,
          snapshot_sha256: document.snapshotSha256,
          structured_head_sha256: document.structuredHeadSha256,
          update_tail_present: false,
          unmaterialized_structured_edits: false,
          diff_available: false,
          can_reimport: true,
          baseline: { available: false },
          source: { available: false },
        });
      }
      throw new Error(`Unexpected request: ${String(input)}`);
    });
    const { container } = render(
      <TriggerHarness
        label="Open re-import"
        dialog={(close) => (
          <CoworkReimportDialog
            storeId={folder.storeId}
            document={document}
            client={new CoworkHttpClient(fetchImpl as typeof fetch)}
            onClose={close}
            onReimported={vi.fn()}
          />
        )}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Open re-import" });
    await user.click(trigger);
    const dialog = await screen.findByRole("dialog", {
      name: "Review external Markdown changes",
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Continue to replacement" })).toBeEnabled(),
    );
    expect(dialog).toContainElement(globalThis.document.activeElement as HTMLElement);
    await expectNoAccessibilityViolations(container);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(trigger).toHaveFocus());
  }, 15_000);

  it("blocks re-import before prepare when local edits are not synced", async () => {
    const user = userEvent.setup();
    const requests: string[] = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      requests.push(url);
      if (url.includes("/drift?")) {
        return json({
          state: "drifted",
          last_materialized_sha256: document.projectionSha256,
          current_file_sha256: document.currentFileSha256,
          snapshot_sha256: document.snapshotSha256,
          structured_head_sha256: document.structuredHeadSha256,
          update_tail_present: false,
          unmaterialized_structured_edits: false,
          diff_available: false,
          can_reimport: true,
          baseline: { available: false },
          source: { available: false },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    render(
      <CoworkReimportDialog
        storeId={folder.storeId}
        document={document}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        localBlockedReason="Sync this document to Co-work before reviewing external changes."
        onClose={vi.fn()}
        onReimported={vi.fn()}
      />,
    );
    const continueButton = await screen.findByRole("button", {
      name: "Continue to replacement",
    });
    await waitFor(() => expect(continueButton).toBeDisabled());
    expect(
      screen.getByText("Sync this document to Co-work before reviewing external changes."),
    ).toBeVisible();
    await user.click(continueButton);
    expect(requests.filter((url) => url.includes("/reimport?"))).toEqual([]);
  });

  it("keeps the prepared removal dialog accessible and restores its trigger", async () => {
    const user = userEvent.setup();
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/retire?")) {
        return json({
          intent_id: "retire-1",
          expires_at: "2026-07-22T20:00:00Z",
          document_id: document.documentId,
          consequence: "The Markdown file and history are retained.",
          consequence_sha256: "5".repeat(64),
        });
      }
      throw new Error(`Unexpected request: ${String(input)}`);
    });
    const { container } = render(
      <TriggerHarness
        label="Open removal"
        dialog={(close) => (
          <CoworkRetirementDialog
            storeId={folder.storeId}
            document={document}
            client={new CoworkHttpClient(fetchImpl as typeof fetch)}
            onClose={close}
            onRetired={vi.fn()}
          />
        )}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Open removal" });
    await user.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "Remove from Co-work?" });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Remove from Co-work" })).toBeEnabled(),
    );
    expect(dialog).toContainElement(globalThis.document.activeElement as HTMLElement);
    await expectNoAccessibilityViolations(container);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(trigger).toHaveFocus());
  }, 15_000);
});
