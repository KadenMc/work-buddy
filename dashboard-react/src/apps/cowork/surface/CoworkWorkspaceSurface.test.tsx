import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, type ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as Y from "yjs";

vi.mock("../../../security/humanAuthority", () => ({
  coworkHumanAuthorityHeaders: vi.fn(async () => ({})),
  exactHumanAuthorityHeaders: vi.fn(async () => ({})),
}));

import {
  asViewId,
  asWidgetInstanceId,
  type WidgetPresentationContext,
} from "../../../dashboard/contributions/contracts";
import { DashboardEventProvider } from "../../../dashboard/events/DashboardEventProvider";
import { fallbackCanvasTheme } from "../../../theme/resolveTheme";
import { expectNoAccessibilityViolations } from "../../../test/setup";
import { DomReviewAnchorController } from "../bridge";
import { CoworkPassageHighlighter } from "../bridge/CoworkPassageHighlighter";
import { LedgerDecorationProjector } from "../bridge/ledgerDecorationProjector";
import {
  COWORK_INTENTS,
  type CoworkDocumentSummary,
  type CoworkWorkspaceInput,
} from "../contracts";
import { saveRailTab } from "../guards";
import { frameSegments } from "../persistence/framing";
import CoworkWorkspaceWidget, {
  reimportReceiptMatchesDocument,
} from "../widget/CoworkWorkspaceWidget";
import {
  coworkProvenanceSelectionActionsActive,
  coworkEditorHelp,
  coworkExecutionSwitchConfirmation,
  resolveFixtureMode,
} from "./CoworkWorkspaceSurface";

/**
 * The composite workspace card is a normal grid widget now, so the tests drive its renderer
 * with the hydrated WidgetRendererProps input the WidgetHost would pass, plus the URL the
 * durable exemption lets it read. The single `<main>` stands in for the grid host that owns
 * the one page landmark, mirroring how the WidgetFrame wraps the card in production.
 */
const presentation: WidgetPresentationContext = {
  instanceId: asWidgetInstanceId("wb-cowork:workspace"),
  viewId: asViewId("wb.cowork.workspace"),
  width: 1280,
  height: 720,
  sizeMode: "expanded",
  interactionMode: "operate",
  editing: false,
  theme: {
    contractVersion: 1,
    preference: { scheme: "light", skinId: "wb.default" },
    resolvedScheme: "light",
    skin: { id: "wb.default", version: 2, publisherAppId: "wb.core" },
    accessibility: {
      forcedColors: false,
      reducedMotion: false,
      reducedTransparency: false,
    },
  },
  getCanvasTheme: () => fallbackCanvasTheme("light"),
};

const noopEmit: ComponentProps<typeof CoworkWorkspaceWidget>["emit"] = async (
  intent,
) => ({ intent_id: intent.intent_id, status: "accepted" });

describe("Provenance selection actions", () => {
  it("stay available in the visible editor pane on narrow workspaces", () => {
    expect(
      coworkProvenanceSelectionActionsActive("provenance", true, "editor"),
    ).toBe(true);
    expect(
      coworkProvenanceSelectionActionsActive("provenance", true, "provenance"),
    ).toBe(false);
    expect(
      coworkProvenanceSelectionActionsActive("provenance", false, "provenance"),
    ).toBe(true);
    expect(coworkProvenanceSelectionActionsActive("chat", true, "editor")).toBe(
      false,
    );
  });
});

const DEMO_DOCUMENT: CoworkDocumentSummary = {
  documentId: "demo-doc",
  path: "docs/demo/co-work-demo.md",
  title: "Co-work demo document",
  profile: "co_authored",
  driftState: "clean",
  openProposalCount: 0,
  openFlagCount: 0,
};

const workspaceElement = (
  input: CoworkWorkspaceInput,
  emit: ComponentProps<typeof CoworkWorkspaceWidget>["emit"] = noopEmit,
) =>
  <DashboardEventProvider>
    <main>
      <CoworkWorkspaceWidget
        input={input}
        emit={emit}
        presentation={presentation}
      />
    </main>
  </DashboardEventProvider>;

const renderWorkspace = (
  input: CoworkWorkspaceInput,
  emit: ComponentProps<typeof CoworkWorkspaceWidget>["emit"] = noopEmit,
) =>
  render(
    workspaceElement(input, emit),
  );

describe("Co-work execution switch impact", () => {
  const candidate = {
    providerId: "codex",
    modelId: "gpt-5.6",
    providerLabel: "Codex",
    modelLabel: "GPT-5.6",
  } as const;

  it("confirms only when switching would restart an active agent", () => {
    expect(
      coworkExecutionSwitchConfirmation("not_started", candidate),
    ).toBeNull();
    expect(
      coworkExecutionSwitchConfirmation("stopped", candidate),
    ).toBeNull();
    expect(
      coworkExecutionSwitchConfirmation("running", candidate),
    ).toEqual({
      title: "Switch to Codex · GPT-5.6?",
      description:
        "This restarts the assistant with the new model. Your messages and draft stay here.",
      confirmLabel: "Switch",
    });
  });
});

describe("Co-work editor hover help", () => {
  it("does not promise source-file Save for a detached import", () => {
    const detached = coworkEditorHelp({ sourceWriteback: "never" });
    expect(detached.details).toContain("file you imported remains unchanged");
    expect(detached.details).not.toContain("Save updates");

    const fileBacked = coworkEditorHelp({ sourceWriteback: "same_file" });
    expect(fileBacked.details).toContain(
      "Save updates the Markdown file in your folder",
    );
  });
});

describe("CoworkWorkspaceWidget default (empty) mode", () => {
  const originalUrl = window.location.href;
  const originalFetch = globalThis.fetch;
  beforeEach(() => window.history.replaceState({}, "", "/app/cowork"));
  afterEach(() => {
    window.history.replaceState({}, "", originalUrl);
    globalThis.fetch = originalFetch;
  });

  const emptyInput: CoworkWorkspaceInput = {
    document: null,
    sessionQuality: "demo",
  };
  const selectedFolder = {
    storeId: "selected-store",
    folderName: "selected-folder",
    folderPath: "C:/Projects/selected-folder",
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
  } as const;

  it("opens with direct Folder selection and the stable toolbar actions", async () => {
    const { container } = renderWorkspace(emptyInput);

    expect(screen.getByRole("button", { name: "Open folder" })).toBeVisible();
    expect(screen.getByRole("button", { name: "New" })).toBeVisible();
    expect(
      screen.getByRole("button", { name: "From file" }),
    ).toBeVisible();
    expect(screen.getByText("No documents yet.")).toBeVisible();
    expect(screen.queryByText("Choose a Folder for Co-work")).toBeNull();
    expect(screen.queryByText(/Folder keeps related documents/i)).toBeNull();
    expect(screen.queryByRole("textbox", { name: /Folder path/i })).toBeNull();
    expect(screen.queryByRole("button", { name: "Inspect Folder" })).toBeNull();
    expect(container.querySelector(".ProseMirror")).toBeNull();
    expect(screen.queryByRole("tab", { name: /Review/ })).toBeNull();
    expect(screen.queryByRole("separator")).toBeNull();
    expect(screen.queryByText(/This is the editor pane/)).toBeNull();
    expect(screen.queryByText("Co-work demo document")).toBeNull();
  }, 15_000);

  it("does not present a failed Folder catalog as an empty Folder", () => {
    const catalogError = {
      code: "request_failed",
      message: "The documents could not be loaded.",
      retryable: true,
    } as const;
    const folderInput: CoworkWorkspaceInput = {
      ...emptyInput,
      folders: [selectedFolder],
      folderSelection: { kind: "initialized", folder: selectedFolder },
      activeFolderStoreId: selectedFolder.storeId,
      catalog: {
        status: "error",
        documents: [],
        refreshedAt: null,
        error: catalogError,
      },
      routeTarget: { kind: "launcher", storeId: selectedFolder.storeId },
    };
    const { rerender } = renderWorkspace(folderInput);

    expect(screen.getByText(catalogError.message)).toBeVisible();
    expect(screen.queryByText("No documents yet.")).toBeNull();

    rerender(
      workspaceElement({
        ...folderInput,
        catalog: {
          ...folderInput.catalog!,
          documents: [DEMO_DOCUMENT],
        },
      }),
    );

    expect(screen.getByText(catalogError.message)).toBeVisible();
    expect(
      screen.getByRole("button", { name: /Co-work demo document/ }),
    ).toBeVisible();
    expect(screen.queryByText("No documents yet.")).toBeNull();
  });

  it("offers a normal new document without exposing its local persistence implementation", async () => {
    const { container } = renderWorkspace(emptyInput);
    expect(screen.getByRole("button", { name: "New" })).toBeVisible();
    expect(screen.queryByText(/scratch/i)).toBeNull();
    expect(container.querySelector(".ProseMirror")).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
    expect(screen.queryByText(/I proposed a few tracked edits/)).toBeNull();
  }, 15_000);

  it("opens not-yet-saved documents from the toolbar without requiring a Folder", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(noopEmit);
    const { container } = renderWorkspace(
      {
        ...emptyInput,
        catalog: {
          status: "ready",
          documents: [DEMO_DOCUMENT],
          refreshedAt: "2026-07-22T00:00:00Z",
          error: null,
        },
        scratches: [
          {
            scratchId: "local-1",
            title: "Untitled",
            createdAt: "2026-07-25T12:00:00Z",
            updatedAt: "2026-07-25T12:05:00Z",
            recoveredFromPreviousEditor: false,
          },
        ],
      },
      emit,
    );

    await user.click(screen.getByRole("button", { name: "Open document" }));
    const option = screen.getByRole("option", {
      name: /Untitled.*Not saved to folder.*Saved in this browser/,
    });
    expect(screen.queryByRole("option", { name: /Co-work demo document/ })).toBeNull();
    expect(
      within(screen.getByRole("dialog", { name: "Open document" })).getByText(
        "Not saved to folder",
      ).tagName,
    ).toBe("EM");
    await user.click(option);

    expect(emit).toHaveBeenCalledWith(
      expect.objectContaining({
        intent_type: COWORK_INTENTS.scratchOpen,
        payload: { scratchId: "local-1" },
      }),
    );
    await expectNoAccessibilityViolations(container);
  });

  it("adds compact parent context only when Folder names collide", () => {
    const permissions = {
      read: true,
      create: true,
      import: true,
      materialize: true,
      retire: true,
    };
    renderWorkspace({
      ...emptyInput,
      folders: [
        {
          storeId: "store-alpha",
          folderName: "work-buddy",
          folderPath: "C:/Projects/alpha/work-buddy",
          layout: "wbuddy_cowork_v1",
          reachable: true,
          eligibility: "eligible",
          ineligibleReason: null,
          permissions,
          documentSurface: {
            enabled: true,
            allowedDocumentClasses: ["co_authored"],
            feedbackCapture: true,
          },
          documentCount: 0,
        },
        {
          storeId: "store-beta",
          folderName: "work-buddy",
          folderPath: "C:/Projects/beta/work-buddy",
          layout: "wbuddy_cowork_v1",
          reachable: true,
          eligibility: "eligible",
          ineligibleReason: null,
          permissions,
          documentSurface: {
            enabled: true,
            allowedDocumentClasses: ["co_authored"],
            feedbackCapture: true,
          },
          documentCount: 0,
        },
      ],
    });

    expect(screen.getByText("alpha")).toBeVisible();
    expect(screen.getByText("beta")).toBeVisible();
    expect(screen.queryByText("C:/Projects/alpha")).toBeNull();
  });

  it("has no accessibility violations in its empty resting state", async () => {
    const { container } = renderWorkspace(emptyInput);
    expect(container.querySelector(".ProseMirror")).toBeNull();
    await expectNoAccessibilityViolations(container);
  }, 15_000);

  it("asks before adding Co-work support data to an ordinary Folder", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: Parameters<typeof noopEmit>[0]) => ({
      intent_id: intent.intent_id,
      status: "accepted" as const,
    }));
    const previousFolder = {
      storeId: "previous-store",
      folderName: "previous",
      folderPath: "C:/Projects/previous",
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
    } as const;
    const { container } = renderWorkspace(
      {
        ...emptyInput,
        folders: [previousFolder],
        activeFolderStoreId: previousFolder.storeId,
        folderSelection: {
          kind: "setup_confirmation",
          candidate: {
            folderName: "work-buddy",
            folderPath: "C:/Projects/work-buddy",
          },
        },
      },
      emit,
    );

    const dialog = screen.getByRole("dialog", {
      name: "Set up Co-work in “work-buddy”?",
    });
    expect(dialog).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "Set up Co-work in “work-buddy”?" }),
    ).toBeVisible();
    expect(screen.getByText(/support data under/)).toHaveTextContent(
      "This adds Co-work support data under .wbuddy. Your documents won’t be changed.",
    );
    expect(screen.getByText("C:/Projects/work-buddy")).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
    const cancel = screen.getByRole("button", { name: "Cancel" });
    expect(cancel).toBeVisible();
    await waitFor(() => expect(cancel).toHaveFocus());
    expect(dialog).toContainElement(globalThis.document.activeElement as HTMLElement);
    expect(
      screen.getByRole("button", { name: "previous", hidden: true }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Open document", hidden: true }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "New", hidden: true }),
    ).toBeDisabled();
    await expectNoAccessibilityViolations(container);

    await user.click(screen.getByRole("button", { name: "Set up Co-work" }));
    expect(emit).toHaveBeenLastCalledWith(
      expect.objectContaining({
        intent_type: COWORK_INTENTS.folderSelect,
        payload: { action: "initialize" },
      }),
    );
  });

  it("explains ordinary Folder setup without offering a mutation in read-only mode", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(noopEmit);
    renderWorkspace(
      {
        ...emptyInput,
        readOnly: true,
        folderSelection: {
          kind: "setup_confirmation",
          candidate: {
            folderName: "work-buddy",
            folderPath: "C:/Projects/work-buddy",
          },
        },
      },
      emit,
    );

    expect(
      screen.getByRole("dialog", {
        name: "Co-work isn’t set up in “work-buddy”",
      }),
    ).toBeVisible();
    expect(
      screen.getByText(/This dashboard is read-only/),
    ).toHaveTextContent(
      "This dashboard is read-only, so Co-work can’t add its support data under .wbuddy. No files were changed. Turn off read-only mode, then open this folder again.",
    );
    expect(
      screen.queryByRole("button", { name: "Set up Co-work" }),
    ).toBeNull();

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(emit).toHaveBeenCalledOnce();
    expect(emit).toHaveBeenCalledWith(
      expect.objectContaining({
        intent_type: COWORK_INTENTS.folderSelect,
        payload: { action: "cancel" },
      }),
    );
  });

  it("uses setup_available only as a concise retry state after confirmed setup fails", () => {
    const { container } = renderWorkspace({
      ...emptyInput,
      folderSelection: {
        kind: "setup_available",
        candidate: {
          folderName: "work-buddy",
          folderPath: "C:/Projects/work-buddy",
        },
      },
    });

    expect(screen.getByText("Co-work couldn’t finish opening work-buddy.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeVisible();
    expect(screen.queryByText(/\.wbuddy|provenance|tool-managed/i)).toBeNull();
    expect(container.querySelector(".ProseMirror")).toBeNull();
    expect(screen.queryByRole("tab", { name: /Review/ })).toBeNull();
  });

  it("serializes a pending Folder open and makes its temporary state obvious", async () => {
    const user = userEvent.setup();
    let release!: () => void;
    const emit = vi.fn(
      (intent: Parameters<typeof noopEmit>[0]) =>
        new Promise<Awaited<ReturnType<typeof noopEmit>>>((resolve) => {
          release = () =>
            resolve({
              intent_id: intent.intent_id,
              status: "accepted" as const,
            });
        }),
    );
    renderWorkspace(
      {
        ...emptyInput,
        activeFolderStoreId: "prior-store",
        folderSelection: {
          kind: "setup_available",
          candidate: {
            folderName: "work-buddy",
            folderPath: "C:/Projects/work-buddy",
          },
        },
      },
      emit,
    );

    await user.click(screen.getByRole("button", { name: "Try again" }));
    const back = screen.getByRole("button", { name: "Back" });
    const actions = back.parentElement;
    expect(actions).not.toBeNull();
    const opening = within(actions!).getByRole("button", { name: "Setting up…" });
    expect(opening).toBeDisabled();
    expect(back).toBeDisabled();
    expect(
      screen.queryByText("Co-work couldn’t finish opening work-buddy."),
    ).toBeNull();
    await user.click(opening);
    expect(emit).toHaveBeenCalledTimes(1);

    release();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled(),
    );
  });

  it("blocks New and document opening while a Folder is opening", async () => {
    const user = userEvent.setup();
    let release!: () => void;
    const emit = vi.fn(
      (intent: Parameters<typeof noopEmit>[0]) =>
        new Promise<Awaited<ReturnType<typeof noopEmit>>>((resolve) => {
          release = () =>
            resolve({
              intent_id: intent.intent_id,
              status: "accepted" as const,
            });
        }),
    );
    const knownFolder = {
      storeId: "recent-store",
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
    } as const;
    renderWorkspace(
      {
        ...emptyInput,
        folders: [knownFolder],
        scratches: [
          {
            scratchId: "scratch-1",
            title: "Untitled",
            createdAt: "2026-07-25T12:00:00Z",
            updatedAt: "2026-07-25T12:00:00Z",
            recoveredFromPreviousEditor: false,
          },
        ],
      },
      emit,
    );

    await user.click(screen.getByRole("button", { name: "work-buddy" }));

    const newDocument = screen.getByRole("button", { name: "New" });
    const continueDocument = screen.getByRole("button", { name: /Untitled.*Not saved/ });
    expect(newDocument).toBeDisabled();
    expect(continueDocument).toBeDisabled();
    await user.click(newDocument);
    await user.click(continueDocument);
    expect(emit).toHaveBeenCalledTimes(1);

    release();
    await waitFor(() => expect(newDocument).toBeEnabled());
  });

  it("keeps creation blocked while replacing an already active Folder", async () => {
    const user = userEvent.setup();
    let release!: () => void;
    const emit = vi.fn(
      (intent: Parameters<typeof noopEmit>[0]) =>
        new Promise<Awaited<ReturnType<typeof noopEmit>>>((resolve) => {
          release = () =>
            resolve({
              intent_id: intent.intent_id,
              status: "accepted" as const,
            });
        }),
    );
    const { rerender } = renderWorkspace(
      {
        ...emptyInput,
        folders: [selectedFolder],
        folderSelection: { kind: "initialized", folder: selectedFolder },
        activeFolderStoreId: selectedFolder.storeId,
        routeTarget: { kind: "launcher", storeId: selectedFolder.storeId },
      },
      emit,
    );

    await user.click(
      screen.getByRole("button", { name: selectedFolder.folderName }),
    );

    const fromFile = screen.getByRole("button", { name: "From file" });
    const newDocument = screen.getByRole("button", { name: "New" });
    expect(fromFile).toBeDisabled();
    expect(newDocument).toBeDisabled();

    const replacementFolder = {
      ...selectedFolder,
      storeId: "replacement-store",
      folderName: "replacement-folder",
      folderPath: "C:/Projects/replacement-folder",
    };
    rerender(
      workspaceElement(
        {
          ...emptyInput,
          folders: [replacementFolder, selectedFolder],
          folderSelection: { kind: "initialized", folder: replacementFolder },
          activeFolderStoreId: replacementFolder.storeId,
          routeTarget: { kind: "launcher", storeId: replacementFolder.storeId },
        },
        emit,
      ),
    );
    expect(fromFile).toBeEnabled();
    expect(newDocument).toBeEnabled();

    release();
    await waitFor(() => expect(fromFile).toBeEnabled());
    expect(newDocument).toBeEnabled();
  });

  it("continues From file after Folder selection without waiting for the Folder request to settle", async () => {
    const user = userEvent.setup();
    let releaseFolder!: () => void;
    const emit = vi.fn(
      (intent: Parameters<typeof noopEmit>[0]) =>
        new Promise<Awaited<ReturnType<typeof noopEmit>>>((resolve) => {
          releaseFolder = () =>
            resolve({
              intent_id: intent.intent_id,
              status: "accepted" as const,
            });
        }),
    );
    globalThis.fetch = vi.fn(() => new Promise<Response>(() => undefined));
    const { rerender } = renderWorkspace(emptyInput, emit);

    await user.click(screen.getByRole("button", { name: "From file" }));
    expect(emit).toHaveBeenCalledOnce();
    expect(emit).toHaveBeenCalledWith(
      expect.objectContaining({
        intent_type: COWORK_INTENTS.folderSelect,
        payload: { action: "choose" },
      }),
    );

    rerender(
      workspaceElement(
        {
          ...emptyInput,
          folders: [selectedFolder],
          folderSelection: { kind: "initialized", folder: selectedFolder },
          activeFolderStoreId: selectedFolder.storeId,
          catalog: {
            status: "loading",
            documents: [],
            refreshedAt: null,
            error: null,
          },
          routeTarget: { kind: "launcher", storeId: selectedFolder.storeId },
        },
        emit,
      ),
    );

    expect(
      await screen.findByRole("dialog", { name: "From file" }),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "From file", hidden: true }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "New", hidden: true }),
    ).toBeEnabled();

    await act(async () => releaseFolder());
  });

  it("clears the retained From file action when Folder selection is cancelled", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(noopEmit);
    const { rerender } = renderWorkspace(emptyInput, emit);

    const fromFile = screen.getByRole("button", { name: "From file" });
    await user.click(fromFile);
    await waitFor(() => expect(fromFile).toBeEnabled());

    rerender(
      workspaceElement(
        {
          ...emptyInput,
          folders: [selectedFolder],
          folderSelection: { kind: "initialized", folder: selectedFolder },
          activeFolderStoreId: selectedFolder.storeId,
          routeTarget: { kind: "launcher", storeId: selectedFolder.storeId },
        },
        emit,
      ),
    );

    expect(screen.queryByRole("dialog", { name: "From file" })).toBeNull();
  });

  it("explains when the selected Folder cannot continue a retained file import", async () => {
    const user = userEvent.setup();
    let releaseFolder!: () => void;
    const emit = vi.fn(
      (intent: Parameters<typeof noopEmit>[0]) =>
        new Promise<Awaited<ReturnType<typeof noopEmit>>>((resolve) => {
          releaseFolder = () =>
            resolve({
              intent_id: intent.intent_id,
              status: "accepted" as const,
            });
        }),
    );
    const blockedFolder = {
      ...selectedFolder,
      permissions: { ...selectedFolder.permissions, import: false },
    };
    const { container, rerender } = renderWorkspace(emptyInput, emit);

    await user.click(screen.getByRole("button", { name: "From file" }));
    rerender(
      workspaceElement(
        {
          ...emptyInput,
          folders: [blockedFolder],
          folderSelection: { kind: "initialized", folder: blockedFolder },
          activeFolderStoreId: blockedFolder.storeId,
          routeTarget: { kind: "launcher", storeId: blockedFolder.storeId },
        },
        emit,
      ),
    );

    await waitFor(() =>
      expect(
        container.querySelector(".wb-cowork-lifecycle__notice"),
      ).toHaveTextContent("This folder doesn’t allow file imports."),
    );
    expect(screen.queryByRole("dialog", { name: "From file" })).toBeNull();

    await act(async () => releaseFolder());
  });

  it("shows a native Folder picker failure once without leaving the start screen", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: Parameters<typeof noopEmit>[0]) => ({
      intent_id: intent.intent_id,
      status: "rejected" as const,
      message: "The folder picker couldn’t be opened.",
    }));
    const { container } = renderWorkspace(emptyInput, emit);

    await user.click(screen.getByRole("button", { name: "Open folder" }));

    expect(
      screen.getAllByText("The folder picker couldn’t be opened."),
    ).toHaveLength(1);
    expect(screen.getByRole("button", { name: "New" })).toBeVisible();
    expect(container.querySelector(".ProseMirror")).toBeNull();
  });

  it("shows one Folder error when saving an on-device document needs setup", async () => {
    const user = userEvent.setup();
    const message = "Co-work couldn’t finish opening work-buddy.";
    const emit = vi.fn(async (intent: Parameters<typeof noopEmit>[0]) => ({
      intent_id: intent.intent_id,
      status:
        intent.intent_type === COWORK_INTENTS.folderSelect
          ? ("rejected" as const)
          : ("accepted" as const),
      ...(intent.intent_type === COWORK_INTENTS.folderSelect ? { message } : {}),
    }));
    const scratchInput: CoworkWorkspaceInput = {
      document: null,
      sessionQuality: "complete",
      scratches: [
        {
          scratchId: "scratch-1",
          title: "Untitled",
          createdAt: "2026-07-25T12:00:00Z",
          updatedAt: "2026-07-25T12:00:00Z",
          recoveredFromPreviousEditor: false,
        },
      ],
      routeTarget: {
        kind: "scratch",
        scratchId: "scratch-1",
        title: "Untitled",
      },
      activeSession: {
        kind: "scratch",
        scratchId: "scratch-1",
        title: "Untitled",
      },
    };
    const { rerender } = renderWorkspace(scratchInput, emit);
    const saveDocument = await screen.findByRole(
      "button",
      { name: "Save document" },
      { timeout: 10_000 },
    );

    await user.click(saveDocument);
    await waitFor(() => expect(screen.getAllByText(message)).toHaveLength(1));

    rerender(
      workspaceElement(
        {
          ...scratchInput,
          folderSelection: {
            kind: "setup_available",
            candidate: {
              folderName: "work-buddy",
              folderPath: "C:/Projects/work-buddy",
            },
          },
          navigationError: {
            code: "setup_failed",
            message,
            retryable: true,
          },
        },
        emit,
      ),
    );

    expect(screen.getAllByText(message)).toHaveLength(1);
  }, 15_000);

  it("does not open Create after Folder selection resolves to a no-create Folder", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(noopEmit);
    const scratchInput: CoworkWorkspaceInput = {
      document: null,
      sessionQuality: "complete",
      scratches: [
        {
          scratchId: "scratch-1",
          title: "Working draft",
          createdAt: "2026-07-25T12:00:00Z",
          updatedAt: "2026-07-25T12:00:00Z",
          recoveredFromPreviousEditor: false,
        },
      ],
      routeTarget: {
        kind: "scratch",
        scratchId: "scratch-1",
        title: "Working draft",
      },
      activeSession: {
        kind: "scratch",
        scratchId: "scratch-1",
        title: "Working draft",
      },
    };
    const { rerender } = renderWorkspace(scratchInput, emit);
    const saveDocument = await screen.findByRole(
      "button",
      { name: "Save document" },
      { timeout: 10_000 },
    );

    await user.click(saveDocument);
    await waitFor(() =>
      expect(emit).toHaveBeenCalledWith(
        expect.objectContaining({
          intent_type: COWORK_INTENTS.folderSelect,
          payload: { action: "choose" },
        }),
      ),
    );

    const blockedFolder = {
      storeId: "blocked-store",
      folderName: "readable-only",
      folderPath: "C:/Projects/readable-only",
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
        create: false,
        import: false,
        materialize: false,
        retire: false,
      },
      documentCount: 0,
    } as const;
    rerender(
      workspaceElement(
        {
          ...scratchInput,
          folders: [blockedFolder],
          folderSelection: { kind: "initialized", folder: blockedFolder },
          activeFolderStoreId: blockedFolder.storeId,
        },
        emit,
      ),
    );

    await waitFor(() =>
      expect(
        screen.getAllByText(
          "This folder doesn’t allow new documents. This document will stay in this browser.",
        ).length,
      ).toBeGreaterThan(0),
    );
    expect(
      screen.getByRole("button", { name: "Save document" }),
    ).toBeDisabled();
    expect(
      screen.queryByRole("dialog", { name: "New document" }),
    ).toBeNull();
    expect(screen.getByText("Working draft")).toBeVisible();
  }, 15_000);

  it("retries a terminal Folder check and can return to the prior Folder", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: Parameters<typeof noopEmit>[0]) => ({
      intent_id: intent.intent_id,
      status: "accepted" as const,
    }));
    renderWorkspace(
      {
        ...emptyInput,
        activeFolderStoreId: "prior-store",
        routeTarget: { kind: "launcher", storeId: "prior-store" },
        folderSelection: {
          kind: "unavailable",
          candidate: {
            folderName: "archive",
            folderPath: "C:/Projects/archive",
          },
          reasonCode: "descendant_scan_incomplete",
          retryable: true,
          availableActions: ["retry"],
        },
        catalog: {
          status: "error",
          documents: [],
          refreshedAt: null,
          error: {
            code: "network_error",
            message: "Documents could not be loaded.",
            retryable: true,
          },
        },
      },
      emit,
    );

    expect(screen.getByRole("button", { name: "Back" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(emit).toHaveBeenLastCalledWith(
      expect.objectContaining({
        intent_type: COWORK_INTENTS.folderSelect,
        payload: { action: "retry" },
      }),
    );

    emit.mockClear();
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(emit).toHaveBeenLastCalledWith(
      expect.objectContaining({
        intent_type: COWORK_INTENTS.folderSelect,
        payload: { action: "cancel" },
      }),
    );
  });

  it("shows a failed owner-Folder open in the terminal Folder screen", async () => {
    const user = userEvent.setup();
    const owner = {
      storeId: "owner-store",
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
    } as const;
    const input: CoworkWorkspaceInput = {
      ...emptyInput,
      folderSelection: {
        kind: "inside_existing_folder",
        candidate: {
          folderName: "notes",
          folderPath: "C:/Projects/work-buddy/notes",
        },
        owner,
      },
    };
    const message = "Work Buddy couldn’t open work-buddy.";
    const emit = vi.fn(async (intent: Parameters<typeof noopEmit>[0]) => ({
      intent_id: intent.intent_id,
      status: "rejected" as const,
      message,
    }));
    const { rerender } = renderWorkspace(input, emit);

    await user.click(screen.getByRole("button", { name: "Open work-buddy" }));
    rerender(
      workspaceElement(
        {
          ...input,
          navigationError: {
            code: "network_error",
            message,
            retryable: true,
          },
        },
        emit,
      ),
    );

    expect(screen.getAllByText(message)).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Open work-buddy" })).toBeVisible();
  });
});

// The demo scene is no longer a product surface (Ruling 1): it is a dev-only fixture entry the
// e2e suites drive against the dev server, gated by import.meta.env.DEV so production tree-shakes
// it. The unit environment runs with DEV true, so ?cowork_fixture=demo still composes the scene
// here exactly as it does for the dev server. The production gate is proven in resolveFixtureMode
// below.
describe("CoworkWorkspaceWidget dev-only demo fixture entry (?cowork_fixture=demo)", () => {
  const originalUrl = window.location.href;
  beforeEach(() =>
    window.history.replaceState({}, "", "/app/cowork?cowork_fixture=demo"),
  );
  afterEach(() => window.history.replaceState({}, "", originalUrl));

  const demoInput: CoworkWorkspaceInput = {
    document: DEMO_DOCUMENT,
    sessionQuality: "demo",
  };

  it("composes the fixture scene behind the dev-only entry", async () => {
    const { container } = renderWorkspace(demoInput);

    const split = container.querySelector(".wb-workspace-side-panel");
    expect(split).toHaveClass("wb-cowork__body");
    expect(split).toHaveAttribute("data-workspace-panel-mode", "split");
    expect(screen.getByTestId("editor").parentElement).toBe(split);
    expect(screen.getByTestId("rail").parentElement).toBe(split);
    expect(screen.getByRole("separator", { name: "Resize the Co-work side panel" }))
      .toHaveClass("wb-workspace-side-panel__separator");

    // Health strip reflects the demo document session.
    await waitFor(
      () => expect(screen.getByText("Co-work demo document")).toBeVisible(),
      { timeout: 10_000 },
    );
    expect(screen.getByText("In sync")).toBeVisible();
    expect(screen.getByText("0 open proposals")).toBeVisible();

    // The demo review rail fixture is present.
    expect(
      screen.getByText("Add the vault content hash to the cache key."),
    ).toBeVisible();

    // The demo editor seeds coherent prose (scoped to the editor, since the rail also
    // quotes these phrases), not the self-describing blurb.
    await waitFor(
      () => expect(container.querySelector(".ProseMirror")).not.toBeNull(),
      { timeout: 10_000 },
    );
    expect(
      within(screen.getByLabelText("Editor")).getByText(/Context bundle cache/),
    ).toBeVisible();
    expect(screen.queryByText(/This is the editor pane/)).toBeNull();
  }, 15_000);

  it("keeps the scripted demo chat behind the dev-only entry", async () => {
    renderWorkspace(demoInput);
    await waitFor(
      () => expect(screen.getByText("Co-work demo document")).toBeVisible(),
      { timeout: 10_000 },
    );

    await userEvent.click(screen.getByRole("tab", { name: /Chat/ }));
    expect(screen.getByRole("tab", { name: /Chat/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await waitFor(
      () =>
        expect(screen.getByText(/I proposed a few tracked edits/)).toBeVisible(),
      { timeout: 10_000 },
    );
  }, 15_000);
});

const LIVE_DOCUMENT: CoworkDocumentSummary = {
  documentId: "live-doc",
  path: "docs/live.md",
  title: "Live doc",
  profile: "co_authored",
  sourceWriteback: "same_file",
  driftState: "clean",
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

const LIVE_FOLDER = {
  storeId: "live-store",
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
} as const;

/** The R2 doc-open payload the stubbed route returns, one edit proposal on the seed text. */
const R2_LIVE_PAYLOAD = {
  document_id: "live-doc",
  store_id: "live-store",
  path: "docs/live.md",
  title: "live.md",
  profile: "co_authored",
  hashes: {
    ydoc_snapshot_sha256: null,
    last_materialized_sha256: null,
    current_file_sha256: "filesha",
  },
  drift: { state: "clean", diff_available: false },
  open_proposals: [
    {
      proposal_id: "s1",
      kind: "edit",
      quote_anchor: { exact: "editor pane", prefix: "This is the ", suffix: "." },
      replacement: "editor pane and its review rail",
      rationale: "Name the rail the pane pairs with.",
      tldr: "Name the review rail.",
      producer: {
        model: "research-agent",
        model_source: "session-manifest",
        session_id: "sess-1",
        surface: "mcp",
      },
      epistemic_state: "ai_proposed",
      base_doc_sha256: "base",
      canonical_sha256: "canon-s1",
      base_ok: true,
      status: "open",
      fixes_ref: null,
      claim_refs: [],
      created_at: "2026-07-17T12:00:00Z",
    },
  ],
  expressions: [],
  provenance_spans: [],
  events_cursor: "c0",
};

const LIVE_CONVERSATION_ID = "7f39ad04bc12";
const LIVE_AGENT = {
  status: "running",
  alive: true,
  started: false,
  error: null,
} as const;

const executionPayload = (
  providerId = "claude-code",
  modelId = "sonnet",
  revision = "",
) => ({
  selection: {
    provider_id: providerId,
    model_id: modelId,
    provider_label: providerId === "codex" ? "Codex" : "Claude Code",
    model_label: modelId === "gpt-5.6" ? "GPT-5.6" : "Sonnet",
    revision,
  },
  providers: [
    {
      id: "claude-code",
      label: "Claude Code",
      available: true,
      availability: "ready",
      auth_mode: "subscription",
      models: [{ id: "sonnet", label: "Sonnet", available: true }],
    },
    {
      id: "codex",
      label: "Codex",
      available: true,
      availability: "ready",
      auth_mode: "chatgpt",
      models: [{ id: "gpt-5.6", label: "GPT-5.6", available: true }],
    },
  ],
  read_only: false,
});

const liveConversationPayload = () => ({
  conversation: {
    conversation_id: LIVE_CONVERSATION_ID,
    title: "Document conversation",
    status: "open",
    agent_alive: true,
  },
  messages: [
    {
      message_id: "agent-1",
      role: "agent",
      content: "I’m ready to work on this document.",
      message_type: "text",
      status: "sent",
    },
  ],
});

const jsonResponse = (body: unknown, status = 200): Response =>
  ({
    ok: status < 400,
    status,
    headers: { get: () => null },
    json: async () => body,
    arrayBuffer: async () => new ArrayBuffer(0),
  }) as unknown as Response;

class MockCoworkEventSource {
  static instances: MockCoworkEventSource[] = [];

  closed = false;
  readonly listeners = new Map<
    string,
    Set<EventListenerOrEventListenerObject>
  >();

  constructor(readonly url: string | URL) {
    MockCoworkEventSource.instances.push(this);
  }

  addEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject,
  ): void {
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject,
  ): void {
    this.listeners.get(type)?.delete(listener);
  }

  close(): void {
    this.closed = true;
  }

  emitMessage(value: unknown): void {
    const event = new MessageEvent<string>("message", {
      data: JSON.stringify(value),
    });
    this.listeners.get("message")?.forEach((listener) => {
      if (typeof listener === "function") listener(event);
      else listener.handleEvent(event);
    });
  }
}

const emptyYdocResponse = (): Response =>
  ({
    ok: true,
    status: 200,
    headers: {
      get: (name: string) =>
        name === "X-WB-Next-Offset" ? "0" : name === "X-WB-Doc-Sha256" ? "h0" : null,
    },
    arrayBuffer: async () => new ArrayBuffer(0),
    json: async () => ({}),
  }) as unknown as Response;

const hydratedYdocResponse = (text: string): Response => {
  const document = new Y.Doc();
  const paragraph = new Y.XmlElement("paragraph");
  paragraph.insert(0, [new Y.XmlText(text)]);
  document.getXmlFragment("default").insert(0, [paragraph]);
  const update = Y.encodeStateAsUpdate(document);
  const body = Uint8Array.from(frameSegments([update])).buffer;
  document.destroy();
  return {
    ok: true,
    status: 200,
    headers: {
      get: (name: string) =>
        name === "X-WB-Next-Offset"
          ? "1"
          : name === "X-WB-Doc-Sha256"
            ? "h1"
            : name === "X-WB-Snapshot-Sha256"
              ? "snapshot-h1"
              : name === "X-WB-Ydoc-Generation"
                ? "generation-h1"
                : null,
    },
    arrayBuffer: async () => body,
    json: async () => ({}),
  } as unknown as Response;
};

const emptyTruthPayload = (url: string) => {
  const params = new URL(url, "https://work-buddy.test").searchParams;
  return {
    schema: "cowork-truth/v1",
    store_id: "live-store",
    document_id: "live-doc",
    view: params.get("view") ?? "document",
    filter: params.get("filter") ?? "all",
    counts: {
      all: 0,
      facts: 0,
      proposed: 0,
      needs_review: 0,
      challenged: 0,
      unconnected: 0,
    },
    capabilities: {
      can_observe: true,
      can_modify: true,
      can_decide: true,
      allowed_claim_kinds: ["fact"],
      mutation_unavailable_reason: null,
    },
    claims: [],
    next_offset: null,
  };
};

const RELATED_DOCUMENT: CoworkDocumentSummary = {
  ...LIVE_DOCUMENT,
  documentId: "related-doc",
  path: "docs/related.md",
  title: "Related doc",
};

const CROSS_DOCUMENT_PASSAGE = "The related document carries this exact passage.";
const CROSS_DOCUMENT_CONNECTION = {
  expression_id: "expression-related",
  span_id: "span-related",
  document_id: RELATED_DOCUMENT.documentId,
  document_title: RELATED_DOCUMENT.title,
  document_path: RELATED_DOCUMENT.path,
  role: "quote",
  quote: CROSS_DOCUMENT_PASSAGE,
  selector: {
    exact: CROSS_DOCUMENT_PASSAGE,
    prefix: "",
    suffix: "",
    start: 0,
    end: CROSS_DOCUMENT_PASSAGE.length,
  },
  claim_canonical_sha256: "claim-cross-canonical",
  created_at: "2026-08-04T12:00:00Z",
  created_by: { kind: "human", ref: "owner" },
} as const;

/** Route the live surface's direct route calls: R2 read, R3 ydoc pull, R4 ydoc push. */
const liveFetch = () =>
  vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (url.includes("/api/truth/doc/live-doc/truth")) {
      return jsonResponse(emptyTruthPayload(url));
    }
    if (url.includes("/api/truth/doc/live-doc/conversation")) {
      return jsonResponse({
        ok: true,
        conversation_id: LIVE_CONVERSATION_ID,
        created: method === "POST",
        agent: LIVE_AGENT,
      });
    }
    if (url.includes(`/api/conversations/${LIVE_CONVERSATION_ID}`)) {
      return jsonResponse(liveConversationPayload());
    }
    if (url.includes("/ydoc")) {
      if (method === "POST") {
        return jsonResponse({ ok: true, applied: true, doc_sha256: "h1", next_offset: "1" });
      }
      return emptyYdocResponse();
    }
    if (url.includes("/api/truth/doc/live-doc")) {
      return jsonResponse(R2_LIVE_PAYLOAD);
    }
    return jsonResponse({ error: "not_found" }, 404);
  });

const crossDocumentInput = (
  document: CoworkDocumentSummary,
): CoworkWorkspaceInput => ({
  document,
  sessionQuality: "complete",
  folders: [LIVE_FOLDER],
  folderSelection: { kind: "initialized", folder: LIVE_FOLDER },
  activeFolderStoreId: LIVE_FOLDER.storeId,
  catalog: {
    status: "ready",
    documents: [LIVE_DOCUMENT, RELATED_DOCUMENT],
    refreshedAt: "2026-08-04T12:00:00Z",
    error: null,
  },
  scratches: [],
  routeTarget: {
    kind: "registered",
    storeId: LIVE_FOLDER.storeId,
    documentId: document.documentId,
  },
  activeSession: {
    kind: "registered",
    storeId: LIVE_FOLDER.storeId,
    document,
  },
  openingTarget: null,
  navigationError: null,
  readOnly: false,
});

const crossDocumentTruthFetch = () => {
  const fallback = liveFetch();
  const currentConnection = {
    ...CROSS_DOCUMENT_CONNECTION,
    expression_id: "expression-live",
    span_id: "span-live",
    document_id: LIVE_DOCUMENT.documentId,
    document_title: LIVE_DOCUMENT.title,
    document_path: LIVE_DOCUMENT.path,
    quote: "The current document expresses this claim.",
    selector: {
      exact: "The current document expresses this claim.",
      prefix: "",
      suffix: "",
      start: 0,
      end: 41,
    },
  };
  const claim = {
    claim_id: "claim-cross",
    proposition: "A claim connected across two documents.",
    claim_kind: "fact",
    canonical_sha256: "claim-cross-canonical",
    scope: "store",
    base_status: "proposed",
    needs_review: false,
    health: "clean",
    voided: false,
    redacted: false,
    is_fact: false,
    receipt_count: 0,
    connection_count: 2,
    document_connections: [currentConnection, CROSS_DOCUMENT_CONNECTION],
    available_actions: [],
    created_at: "2026-08-04T12:00:00Z",
  };
  return vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.includes("/api/truth/doc/live-doc/truth/claims/claim-cross")) {
        return jsonResponse({
          claim,
          connections: [currentConnection, CROSS_DOCUMENT_CONNECTION],
          status_history: [],
          receipts: [],
          conflicts: [],
          derivations: [],
          decision_binding: {
            payload_sha256: "payload-cross",
            context_sha256: "context-cross",
            agent_authored_only: false,
          },
        });
      }
      if (url.includes("/api/truth/doc/live-doc/truth")) {
        const payload = emptyTruthPayload(url);
        return jsonResponse({
          ...payload,
          counts: { ...payload.counts, all: 1, proposed: 1 },
          claims: [claim],
        });
      }
      if (url.includes("/api/truth/doc/related-doc/truth")) {
        return jsonResponse({
          ...emptyTruthPayload(url),
          document_id: RELATED_DOCUMENT.documentId,
        });
      }
      if (url.includes("/api/truth/doc/related-doc/conversation")) {
        return jsonResponse({
          ok: true,
          conversation_id: LIVE_CONVERSATION_ID,
          created: method === "POST",
          agent: LIVE_AGENT,
        });
      }
      if (url.includes("/ydoc")) return hydratedYdocResponse(CROSS_DOCUMENT_PASSAGE);
      if (url.includes("/api/truth/doc/related-doc")) {
        return jsonResponse({
          ...R2_LIVE_PAYLOAD,
          document_id: RELATED_DOCUMENT.documentId,
          path: RELATED_DOCUMENT.path,
          title: RELATED_DOCUMENT.title,
          open_proposals: [],
          expressions: [{
            expression_id: CROSS_DOCUMENT_CONNECTION.expression_id,
            span_id: CROSS_DOCUMENT_CONNECTION.span_id,
            node_id_hint: null,
            quote: CROSS_DOCUMENT_PASSAGE,
            quote_anchor: CROSS_DOCUMENT_CONNECTION.selector,
            claim_ref: "claim-cross",
            claim_status: "proposed",
            claim_kind: "fact",
          }],
        });
      }
      return fallback(input, init);
    },
  );
};

describe("CoworkWorkspaceWidget live mode", () => {
  const originalFetch = globalThis.fetch;
  const originalUrl = window.location.href;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    window.history.replaceState({}, "", originalUrl);
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  const renderLive = (
    emit: ComponentProps<typeof CoworkWorkspaceWidget>["emit"] = noopEmit,
    fetchImpl: typeof fetch = liveFetch() as unknown as typeof fetch,
  ) => {
    window.history.replaceState({}, "", "/app/cowork?store_id=live-store&document_id=live-doc");
    globalThis.fetch = fetchImpl;
    return renderWorkspace({
      document: LIVE_DOCUMENT,
      sessionQuality: "complete",
      folders: [LIVE_FOLDER],
      folderSelection: { kind: "initialized", folder: LIVE_FOLDER },
      activeFolderStoreId: LIVE_FOLDER.storeId,
      catalog: {
        status: "ready",
        documents: [LIVE_DOCUMENT],
        refreshedAt: "2026-07-22T00:00:00Z",
        error: null,
      },
      scratches: [],
      routeTarget: {
        kind: "registered",
        storeId: LIVE_FOLDER.storeId,
        documentId: LIVE_DOCUMENT.documentId,
      },
      activeSession: {
        kind: "registered",
        storeId: LIVE_FOLDER.storeId,
        document: LIVE_DOCUMENT,
      },
      openingTarget: null,
      navigationError: null,
      readOnly: false,
    }, emit);
  };

  it("places the Verify and Co-think dock across the workspace", async () => {
    const { container } = renderLive();

    await screen.findByRole("button", { name: "Verify" });
    const dock = container.querySelector(".wb-cowork-action-dock");

    expect(dock).not.toBeNull();
    expect(dock?.parentElement).toHaveClass("wb-cowork");
    expect(dock?.closest(".wb-cowork__editor-panel")).toBeNull();
  });

  it("maps Review, Truth, and Chat selection to the editor's view-only lens", async () => {
    const setLens = vi.spyOn(LedgerDecorationProjector.prototype, "setLens");
    const user = userEvent.setup();
    renderLive();

    await waitFor(() => expect(setLens).toHaveBeenCalledWith("review"));
    await user.click(screen.getByRole("tab", { name: "Truth" }));
    await waitFor(() => expect(setLens).toHaveBeenLastCalledWith("truth"));

    await user.click(screen.getByRole("tab", { name: /Chat/ }));
    await waitFor(() => expect(setLens).toHaveBeenLastCalledWith("neutral"));

    await user.click(screen.getByRole("tab", { name: "Review" }));
    await waitFor(() => expect(setLens).toHaveBeenLastCalledWith("review"));
  });

  it("opens a connected document and reveals its exact Truth passage once when effects replay", async () => {
    window.localStorage.clear();
    const showPassage = vi.spyOn(
      CoworkPassageHighlighter.prototype,
      "show",
    ).mockReturnValue(true);
    const focusAnchor = vi.spyOn(
      DomReviewAnchorController.prototype,
      "focusAnchor",
    );
    const emit = vi.fn(noopEmit);
    const fetchImpl = crossDocumentTruthFetch();
    globalThis.fetch = fetchImpl as unknown as typeof fetch;
    window.history.replaceState(
      {},
      "",
      "/app/cowork?store_id=live-store&document_id=live-doc",
    );
    const { container, rerender } = render(
      <StrictMode>
        {workspaceElement(crossDocumentInput(LIVE_DOCUMENT), emit)}
      </StrictMode>,
    );

    await userEvent.click(await screen.findByRole("tab", { name: "Truth" }));
    await userEvent.click(await screen.findByRole("button", {
      name: "A claim connected across two documents.",
    }));
    await userEvent.click(await screen.findByRole("button", {
      name: "Open and show passage",
    }));

    expect(emit).toHaveBeenCalledWith(expect.objectContaining({
      intent_type: COWORK_INTENTS.documentOpen,
      payload: {
        storeId: LIVE_FOLDER.storeId,
        documentId: RELATED_DOCUMENT.documentId,
      },
    }));

    window.history.replaceState(
      {},
      "",
      "/app/cowork?store_id=live-store&document_id=related-doc",
    );
    rerender(
      <StrictMode>
        {workspaceElement(crossDocumentInput(RELATED_DOCUMENT), emit)}
      </StrictMode>,
    );

    await waitFor(
      () => expect(container.querySelector(".ProseMirror")).not.toBeNull(),
      { timeout: 10_000 },
    );
    await waitFor(() => expect(
      focusAnchor.mock.calls.filter(
        ([id, kind]) =>
          id === CROSS_DOCUMENT_CONNECTION.expression_id && kind === "expression",
      ),
    ).toHaveLength(1), { timeout: 10_000 });
    await waitFor(() => expect(showPassage).toHaveBeenCalledWith({
      spanId: CROSS_DOCUMENT_CONNECTION.span_id,
      anchor: {
        exact: CROSS_DOCUMENT_PASSAGE,
        prefix: "",
        suffix: "",
      },
    }), { timeout: 10_000 });
    expect(screen.getByRole("tab", { name: "Truth" })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    // Rerendering the destination (and any later ledger refresh it causes) must
    // preserve focus without replaying the user-commanded scroll/highlight.
    rerender(
      <StrictMode>
        {workspaceElement(crossDocumentInput(RELATED_DOCUMENT), emit)}
      </StrictMode>,
    );
    await act(async () => Promise.resolve());
    expect(showPassage).toHaveBeenCalledTimes(1);
    saveRailTab(window.localStorage, LIVE_DOCUMENT.documentId, "review", LIVE_FOLDER.storeId);
    saveRailTab(window.localStorage, RELATED_DOCUMENT.documentId, "review", LIVE_FOLDER.storeId);
  }, 15_000);

  it("discards a cross-document Truth reveal when opening the document fails", async () => {
    window.localStorage.clear();
    const showPassage = vi.spyOn(
      CoworkPassageHighlighter.prototype,
      "show",
    ).mockReturnValue(true);
    const emit = vi.fn(async (intent: Parameters<typeof noopEmit>[0]) =>
      intent.intent_type === COWORK_INTENTS.documentOpen
        ? {
            intent_id: intent.intent_id,
            status: "rejected" as const,
            message: "The connected document is unavailable.",
          }
        : {
            intent_id: intent.intent_id,
            status: "accepted" as const,
          },
    );
    globalThis.fetch = crossDocumentTruthFetch() as unknown as typeof fetch;
    const { container, rerender } = renderWorkspace(
      crossDocumentInput(LIVE_DOCUMENT),
      emit,
    );

    await userEvent.click(await screen.findByRole("tab", { name: "Truth" }));
    await userEvent.click(await screen.findByRole("button", {
      name: "A claim connected across two documents.",
    }));
    await userEvent.click(await screen.findByRole("button", {
      name: "Open and show passage",
    }));
    expect(await screen.findByText("The connected document is unavailable.")).toBeVisible();

    // A later ordinary visit to the same document cannot replay the failed request.
    showPassage.mockClear();
    rerender(workspaceElement(crossDocumentInput(RELATED_DOCUMENT), emit));
    await waitFor(
      () => expect(container.querySelector(".ProseMirror")).not.toBeNull(),
      { timeout: 10_000 },
    );
    expect(showPassage).not.toHaveBeenCalled();
    saveRailTab(window.localStorage, LIVE_DOCUMENT.documentId, "review", LIVE_FOLDER.storeId);
    saveRailTab(window.localStorage, RELATED_DOCUMENT.documentId, "review", LIVE_FOLDER.storeId);
  }, 15_000);

  it("fans a Truth event out to both Review and Truth authoritative reads", async () => {
    MockCoworkEventSource.instances = [];
    vi.stubGlobal(
      "EventSource",
      MockCoworkEventSource as unknown as typeof EventSource,
    );
    const fallback = liveFetch();
    let reviewReads = 0;
    let truthReads = 0;
    const fetchImpl = vi.fn(
      async (
        input: RequestInfo | URL,
        init?: RequestInit,
      ): Promise<Response> => {
        const url = String(input);
        if (url.includes("/api/truth/doc/live-doc/truth")) {
          truthReads += 1;
        } else if (
          url === "/api/truth/doc/live-doc?store_id=live-store" &&
          (init?.method ?? "GET") === "GET"
        ) {
          reviewReads += 1;
        }
        return fallback(input, init);
      },
    );
    renderLive(noopEmit, fetchImpl as unknown as typeof fetch);

    await waitFor(() => expect(MockCoworkEventSource.instances).toHaveLength(1));
    await waitFor(() => expect(reviewReads).toBeGreaterThan(0));
    await waitFor(() => expect(truthReads).toBeGreaterThan(0));
    const initialReviewReads = reviewReads;
    const initialTruthReads = truthReads;

    act(() => {
      MockCoworkEventSource.instances[0]?.emitMessage({
        event_type: "truth.claim_confirmed",
        payload: { event_id: "truth-event-1" },
        ts: 1_786_000_000,
      });
    });

    await waitFor(() => expect(reviewReads).toBeGreaterThan(initialReviewReads));
    await waitFor(() => expect(truthReads).toBeGreaterThan(initialTruthReads));
  }, 15_000);

  it("routes Review card activation to a one-shot editor reveal", async () => {
    const revealAnchor = vi.spyOn(
      DomReviewAnchorController.prototype,
      "revealAnchor",
    );
    renderLive();
    await waitFor(
      () => expect(screen.getByText("Name the review rail.")).toBeVisible(),
      { timeout: 10_000 },
    );

    await userEvent.click(screen.getByText("Name the review rail."));

    await waitFor(() =>
      expect(revealAnchor).toHaveBeenCalledWith("s1", "proposal", undefined),
    );
  });

  it("loads and reloads the exact server-issued conversation id", async () => {
    const firstFetch = liveFetch();
    const first = renderLive(
      noopEmit,
      firstFetch as unknown as typeof fetch,
    );

    await waitFor(() =>
      expect(
        firstFetch.mock.calls.some(
          ([input, init]) =>
            String(input) ===
              "/api/truth/doc/live-doc/conversation?store_id=live-store" &&
            (init?.method ?? "GET") === "GET",
        ),
      ).toBe(true),
    );
    await waitFor(() =>
      expect(
        firstFetch.mock.calls.some(
          ([input]) =>
            String(input) ===
            `/api/conversations/${LIVE_CONVERSATION_ID}`,
        ),
      ).toBe(true),
    );
    expect(
      firstFetch.mock.calls.some(([input]) =>
        String(input).includes("/api/conversations/cowork-doc-"),
      ),
    ).toBe(false);
    await userEvent.click(screen.getByRole("tab", { name: /Chat/ }));
    expect(
      firstFetch.mock.calls.filter(
        ([input, init]) =>
          String(input).includes("/conversation?store_id=live-store") &&
          init?.method === "POST",
      ),
    ).toHaveLength(0);
    first.unmount();

    const reloadFetch = liveFetch();
    renderLive(noopEmit, reloadFetch as unknown as typeof fetch);
    await waitFor(() =>
      expect(
        reloadFetch.mock.calls.some(
          ([input]) =>
            String(input) ===
            `/api/conversations/${LIVE_CONVERSATION_ID}`,
        ),
      ).toBe(true),
    );
    expect(
      reloadFetch.mock.calls.filter(
        ([input, init]) =>
          String(input).includes("/conversation?store_id=live-store") &&
          init?.method === "POST",
      ),
    ).toHaveLength(0);
    saveRailTab(window.localStorage, "live-doc", "review", "live-store");
  });

  it("restores a persisted Chat view with GET only", async () => {
    saveRailTab(window.localStorage, "live-doc", "chat", "live-store");
    const fetchImpl = liveFetch();
    renderLive(noopEmit, fetchImpl as unknown as typeof fetch);

    expect(
      await screen.findByText("I’m ready to work on this document."),
    ).toBeVisible();
    expect(
      fetchImpl.mock.calls.filter(
        ([input, init]) =>
          String(input).includes("/conversation?store_id=live-store") &&
          init?.method === "POST",
      ),
    ).toHaveLength(0);
    saveRailTab(window.localStorage, "live-doc", "review", "live-store");
  });

  it("keeps a stopped Chat writable without exposing restart controls", async () => {
    const fallback = liveFetch();
    let lifecyclePosts = 0;
    const fetchImpl = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        const url = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        if (
          url.includes(
            "/api/truth/doc/live-doc/conversation?store_id=live-store",
          )
        ) {
          if (method === "POST") lifecyclePosts += 1;
          return jsonResponse({
            ok: true,
            conversation_id: LIVE_CONVERSATION_ID,
            created: false,
            agent: {
              status: "stopped",
              alive: false,
              started: true,
              error: null,
            },
          });
        }
        if (url === `/api/conversations/${LIVE_CONVERSATION_ID}`) {
          return jsonResponse({
            conversation: {
              conversation_id: LIVE_CONVERSATION_ID,
              title: "Document conversation",
              status: "open",
              agent_alive: false,
            },
            messages: [],
          });
        }
        return fallback(input, init);
      },
    );
    renderLive(noopEmit, fetchImpl as unknown as typeof fetch);

    await userEvent.click(screen.getByRole("tab", { name: /Chat/ }));
    expect(await screen.findByPlaceholderText("Type a message…")).toBeEnabled();
    expect(screen.queryByRole("button", { name: /Restart chat/i })).toBeNull();
    expect(screen.queryByText("Chat paused.")).toBeNull();
    expect(lifecyclePosts).toBe(0);
    saveRailTab(window.localStorage, "live-doc", "review", "live-store");
  });

  it("hydrates persisted feedback span links on reload by exact message id", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: query === "(max-width: 760px)",
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(() => true),
      })),
    );
    const frameSpy = vi
      .spyOn(window, "requestAnimationFrame")
      .mockImplementation((callback) =>
        window.setTimeout(() => callback(performance.now()), 0),
      );
    saveRailTab(window.localStorage, "live-doc", "chat", "live-store");
    const persistedFeedback = {
      evidence_id: "feedback-evidence-1",
      span_id: "feedback-span-1",
      conversation_id: LIVE_CONVERSATION_ID,
      message_id: "feedback-message-1",
      text: "Use a measurable claim.",
      anchor: {
        exact: "editor pane",
        prefix: "This is ",
        suffix: ".",
        node_id_hint: null,
      },
    };
    const feedbackFetch = () => {
      const fallback = liveFetch();
      return vi.fn(
        async (
          input: RequestInfo | URL,
          init?: RequestInit,
        ): Promise<Response> => {
          const url = String(input);
          if (url.includes("/api/truth/doc/live-doc/conversation")) {
            return jsonResponse({
              ok: true,
              conversation_id: LIVE_CONVERSATION_ID,
              created: false,
              agent: LIVE_AGENT,
              feedback: [persistedFeedback],
            });
          }
          if (url === `/api/conversations/${LIVE_CONVERSATION_ID}`) {
            return jsonResponse({
              conversation: {
                conversation_id: LIVE_CONVERSATION_ID,
                title: "Chat about this document",
                status: "open",
                agent_alive: true,
              },
              messages: [
                {
                  message_id: "feedback-message-1",
                  role: "user",
                  content: "Use a measurable claim.",
                },
              ],
            });
          }
          return fallback(input, init);
        },
      );
    };

    const firstFetch = feedbackFetch();
    const first = renderLive(
      noopEmit,
      firstFetch as unknown as typeof fetch,
    );
    const firstPaneTabs = screen.getByRole("tablist", {
      name: "Co-work panes",
    });
    await userEvent.click(
      within(firstPaneTabs).getByRole("tab", { name: "Chat" }),
    );
    expect(
      await screen.findByRole("button", {
        name: 'Jump to passage: "editor pane"',
      }),
    ).toBeVisible();
    first.unmount();

    const reloadFetch = feedbackFetch();
    renderLive(noopEmit, reloadFetch as unknown as typeof fetch);
    const paneTabs = screen.getByRole("tablist", { name: "Co-work panes" });
    const editorTab = within(paneTabs).getByRole("tab", { name: "Editor" });
    const chatTab = within(paneTabs).getByRole("tab", { name: "Chat" });
    await userEvent.click(chatTab);
    const jump = await screen.findByRole("button", {
      name: 'Jump to passage: "editor pane"',
    });
    expect(jump).toBeVisible();
    expect(chatTab).toHaveAttribute("aria-selected", "true");
    const framesBeforeJump = frameSpy.mock.calls.length;
    await userEvent.click(jump);
    await waitFor(() => expect(editorTab).toHaveAttribute("aria-selected", "true"));
    await waitFor(() => expect(editorTab).toHaveFocus());
    expect(screen.getByText("That passage could not be found.")).toBeInTheDocument();
    expect(
      document.querySelector('[data-wb-anchor-id="feedback:feedback-span-1"]'),
    ).toBeNull();
    expect(frameSpy.mock.calls.length).toBeGreaterThan(framesBeforeJump);
    expect(
      reloadFetch.mock.calls.filter(
        ([input, init]) =>
          String(input).includes("/conversation?store_id=live-store") &&
          init?.method === "POST",
      ),
    ).toHaveLength(0);
    saveRailTab(window.localStorage, "live-doc", "review", "live-store");
  });

  it("prepares from Chat selection, then loads the returned opaque id", async () => {
    const baseFetch = liveFetch();
    const ensuredId = "server-issued-after-click-72";
    const fetchImpl = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        const url = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        if (
          url.includes(
            "/api/truth/doc/live-doc/conversation?store_id=live-store",
          )
        ) {
          if (method === "GET") {
            return jsonResponse({
              ok: true,
              conversation_id: null,
              agent: {
                status: "not_started",
                alive: null,
                started: false,
                error: null,
              },
            });
          }
        }
        if (
          url.includes(
            "/api/truth/doc/live-doc/conversation/bind?store_id=live-store",
          )
        ) {
          return jsonResponse({
            ok: true,
            conversation_id: ensuredId,
            created: true,
            agent: {
              status: "not_started",
              alive: null,
              started: false,
              error: null,
            },
          });
        }
        if (url === `/api/conversations/${ensuredId}`) {
          return jsonResponse({
            conversation: {
              conversation_id: ensuredId,
              title: "Chat about this document",
              status: "open",
              agent_alive: true,
            },
            messages: [
              {
                message_id: "agent-click",
                role: "agent",
                content: "Chat started from this click.",
              },
            ],
          });
        }
        return baseFetch(input, init);
      },
    );
    renderLive(noopEmit, fetchImpl as unknown as typeof fetch);

    await waitFor(() =>
      expect(
        fetchImpl.mock.calls.some(
          ([input, init]) =>
            String(input).includes("/conversation?store_id=live-store") &&
            init?.method === "GET",
        ),
      ).toBe(true),
    );
    expect(
      fetchImpl.mock.calls.some(([input]) =>
        String(input).startsWith("/api/conversations/"),
      ),
    ).toBe(false);

    await userEvent.click(screen.getByRole("tab", { name: /Chat/ }));
    expect(await screen.findByText("Chat started from this click.")).toBeVisible();
    expect(
      fetchImpl.mock.calls.some(
        ([input, init]) =>
          String(input).includes("/conversation/bind?store_id=live-store") &&
          init?.method === "POST",
      ),
    ).toBe(true);
    expect(screen.queryByRole("button", { name: /Start chat/i })).toBeNull();
    expect(
      fetchImpl.mock.calls.some(
        ([input]) => String(input) === `/api/conversations/${ensuredId}`,
      ),
    ).toBe(true);
    expect(
      fetchImpl.mock.calls.some(([input]) =>
        String(input).includes("/api/conversations/cowork-doc-"),
      ),
    ).toBe(false);
    saveRailTab(window.localStorage, "live-doc", "review", "live-store");
  });

  it("selects a model after preparation without sending lifecycle instructions", async () => {
    const baseFetch = liveFetch();
    const startedConversationId = "server-issued-codex-chat";
    const patchBodies: unknown[] = [];
    const bindRequests: RequestInit[] = [];
    let execution = executionPayload();
    const fetchImpl = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        const url = String(input);
        if (
          url.includes(
            "/api/truth/doc/live-doc/conversation/execution?store_id=live-store",
          )
        ) {
          patchBodies.push(JSON.parse(String(init?.body)));
          execution = executionPayload(
            "codex",
            "gpt-5.6",
            "execution:codex",
          );
          return jsonResponse({
            ok: true,
            conversation_id: startedConversationId,
            execution,
            agent: {
              status: "not_started",
              alive: null,
              started: false,
              error: null,
            },
          });
        }
        if (
          url.includes(
            "/api/truth/doc/live-doc/conversation/bind?store_id=live-store",
          )
        ) {
          bindRequests.push(init ?? {});
          return jsonResponse({
            ok: true,
            conversation_id: startedConversationId,
            created: true,
            execution,
            agent: {
              status: "not_started",
              alive: null,
              started: false,
              error: null,
            },
          });
        }
        if (
          url.includes(
            "/api/truth/doc/live-doc/conversation?store_id=live-store",
          )
        ) {
          return jsonResponse({
            ok: true,
            conversation_id: null,
            created: false,
            execution,
            agent: {
              status: "not_started",
              alive: null,
              started: false,
              error: null,
            },
          });
        }
        if (url === `/api/conversations/${startedConversationId}`) {
          return jsonResponse({
            conversation: {
              conversation_id: startedConversationId,
              title: "Chat about this document",
              status: "open",
              agent_alive: null,
            },
            messages: [],
          });
        }
        return baseFetch(input, init);
      },
    );
    renderLive(noopEmit, fetchImpl as unknown as typeof fetch);

    await userEvent.click(screen.getByRole("tab", { name: /Chat/ }));
    const trigger = await screen.findByRole("button", {
      name: "Run with Claude Code · Sonnet",
    });
    await userEvent.click(trigger);
    await userEvent.click(
      screen.getByRole("option", { name: "Codex, GPT-5.6" }),
    );

    await waitFor(() =>
      expect(
        screen.getByRole("button", {
          name: "Run with Codex · GPT-5.6",
        }),
      ).toBeVisible(),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(patchBodies).toEqual([
      {
        provider_id: "codex",
        model_id: "gpt-5.6",
        expected_revision: "",
      },
    ]);

    expect(bindRequests).toHaveLength(1);
    expect(bindRequests[0]?.body).toBeUndefined();
    expect(screen.queryByRole("button", { name: /Start chat/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /Restart chat/i })).toBeNull();
    saveRailTab(window.localStorage, "live-doc", "review", "live-store");
  });

  it("filters Documents to the open folder and shows browser-local work only without one", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(noopEmit);
    const localDocument = {
      scratchId: "local-1",
      title: "Untitled",
      createdAt: "2026-07-25T12:00:00Z",
      updatedAt: "2026-07-25T12:05:00Z",
      recoveredFromPreviousEditor: false,
    } as const;
    window.history.replaceState({}, "", "/app/cowork?store_id=live-store");
    const { container, rerender } = renderWorkspace(
      {
        document: null,
        sessionQuality: "complete",
        folders: [LIVE_FOLDER],
        folderSelection: { kind: "initialized", folder: LIVE_FOLDER },
        activeFolderStoreId: LIVE_FOLDER.storeId,
        catalog: {
          status: "ready",
          documents: [LIVE_DOCUMENT],
          refreshedAt: "2026-07-22T00:00:00Z",
          error: null,
        },
        scratches: [localDocument],
        routeTarget: { kind: "launcher", storeId: LIVE_FOLDER.storeId },
        activeSession: { kind: "none" },
        openingTarget: null,
        navigationError: null,
        readOnly: false,
      },
      emit,
    );

    expect(screen.getAllByRole("heading", { name: "Documents" })).toHaveLength(1);
    expect(screen.queryByRole("heading", { name: "Recent documents" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "On this device" })).toBeNull();
    const folderDocument = screen.getByRole("button", {
      name: /Live doc.*work-buddy.*docs\/live\.md/,
    });
    expect(screen.queryByRole("button", { name: /Untitled/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Continue" })).toBeNull();

    await user.click(folderDocument);
    expect(emit).toHaveBeenCalledWith(
      expect.objectContaining({
        intent_type: COWORK_INTENTS.documentOpen,
        payload: {
          storeId: LIVE_FOLDER.storeId,
          documentId: LIVE_DOCUMENT.documentId,
        },
      }),
    );

    rerender(
      workspaceElement(
        {
          document: null,
          sessionQuality: "complete",
          folders: [LIVE_FOLDER],
          folderSelection: { kind: "none" },
          activeFolderStoreId: null,
          catalog: {
            status: "ready",
            documents: [],
            refreshedAt: null,
            error: null,
          },
          scratches: [localDocument],
          routeTarget: { kind: "launcher", storeId: null },
          activeSession: { kind: "none" },
          openingTarget: null,
          navigationError: null,
          readOnly: false,
        },
        emit,
      ),
    );

    expect(screen.queryByRole("button", { name: /Live doc/ })).toBeNull();
    const browserLocalDocument = screen.getByRole("button", {
      name: /Untitled.*Not saved to folder.*Saved in this browser/,
    });
    const notSaved = screen.getByText("Not saved to folder");
    expect(notSaved.tagName).toBe("EM");
    await user.click(browserLocalDocument);

    expect(emit).toHaveBeenCalledWith(
      expect.objectContaining({
        intent_type: COWORK_INTENTS.scratchOpen,
        payload: { scratchId: "local-1" },
      }),
    );
    await expectNoAccessibilityViolations(container);
  });

  it("keeps creation in the toolbar and exposes a real Close Folder action", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(noopEmit);
    window.history.replaceState({}, "", "/app/cowork?store_id=live-store");
    renderWorkspace(
      {
        document: null,
        sessionQuality: "complete",
        folders: [LIVE_FOLDER],
        folderSelection: { kind: "initialized", folder: LIVE_FOLDER },
        activeFolderStoreId: LIVE_FOLDER.storeId,
        catalog: {
          status: "ready",
          documents: [LIVE_DOCUMENT],
          refreshedAt: "2026-07-22T00:00:00Z",
          error: null,
        },
        scratches: [],
        routeTarget: { kind: "launcher", storeId: LIVE_FOLDER.storeId },
        activeSession: { kind: "none" },
        openingTarget: null,
        navigationError: null,
        readOnly: false,
      },
      emit,
    );

    const launcher = screen.getByRole("region", {
      name: `${LIVE_FOLDER.folderName} documents`,
    });
    expect(within(launcher).queryByRole("button", { name: "New" })).toBeNull();
    expect(
      within(launcher).queryByRole("button", { name: "From file" }),
    ).toBeNull();
    expect(screen.getByRole("button", { name: "New" })).toBeVisible();
    expect(screen.getByRole("button", { name: "From file" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Close folder" }));
    expect(emit).toHaveBeenCalledWith(
      expect.objectContaining({
        intent_type: COWORK_INTENTS.folderClose,
        payload: {},
      }),
    );
  });

  it("remounts a replacement only after the clean catalog matches its commit receipt", () => {
    const receipt = {
      intentId: "reimport-1",
      documentId: LIVE_DOCUMENT.documentId,
      sourceSha256: "source-v2",
      snapshotSha256: "snapshot-v2",
      structuredHeadSha256: "head-v2",
      documentVersionId: "version-v2",
      docEventId: "event-v2",
      staledProposalIds: [],
      reimportedAt: "2026-07-22T19:00:00Z",
    };
    const committedDocument: CoworkDocumentSummary = {
      ...LIVE_DOCUMENT,
      currentFileSha256: receipt.sourceSha256,
      snapshotSha256: receipt.snapshotSha256,
      structuredHeadSha256: receipt.structuredHeadSha256,
    };

    expect(
      reimportReceiptMatchesDocument(receipt, {
        ...committedDocument,
        driftState: "drifted",
      }),
    ).toBe(false);
    expect(
      reimportReceiptMatchesDocument(receipt, {
        ...committedDocument,
        structuredHeadSha256: "stale-head",
      }),
    ).toBe(false);
    expect(reimportReceiptMatchesDocument(receipt, committedDocument)).toBe(true);
  });

  it("keeps From file unavailable when the host cannot open the importer", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(noopEmit);
    window.history.replaceState({}, "", "/app/cowork?store_id=live-store");
    renderWorkspace(
      {
        document: null,
        sessionQuality: "complete",
        folders: [LIVE_FOLDER],
        folderChooser: {
          available: true,
          kind: "host_native",
          importAvailable: false,
          locationAvailable: true,
        },
        folderSelection: { kind: "initialized", folder: LIVE_FOLDER },
        activeFolderStoreId: LIVE_FOLDER.storeId,
        catalog: {
          status: "ready",
          documents: [LIVE_DOCUMENT],
          refreshedAt: "2026-07-22T00:00:00Z",
          error: null,
        },
        scratches: [],
        routeTarget: { kind: "launcher", storeId: LIVE_FOLDER.storeId },
        activeSession: { kind: "none" },
        openingTarget: null,
        navigationError: null,
        readOnly: false,
      },
      emit,
    );

    const fromFile = screen.getByRole("button", {
      name: "From file",
    });
    expect(fromFile).toHaveAttribute("aria-disabled", "true");
    expect(fromFile).toBeEnabled();
    fromFile.focus();
    expect(fromFile).toHaveFocus();
    expect(fromFile).toHaveAccessibleDescription(
      "File import isn’t available here.",
    );
    expect(
      screen.getByText("File import isn’t available here."),
    ).toBeVisible();

    await user.click(fromFile);
    expect(
      screen.queryByRole("dialog", { name: "From file" }),
    ).toBeNull();
    expect(emit).not.toHaveBeenCalled();
  });

  it("opens the document picker instead of guessing which existing document to open", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: Parameters<typeof noopEmit>[0]) => ({
      intent_id: intent.intent_id,
      status: "accepted" as const,
    }));
    window.history.replaceState({}, "", "/app/cowork?store_id=live-store");
    renderWorkspace(
      {
        document: null,
        sessionQuality: "complete",
        folders: [LIVE_FOLDER],
        folderSelection: { kind: "initialized", folder: LIVE_FOLDER },
        activeFolderStoreId: LIVE_FOLDER.storeId,
        catalog: {
          status: "ready",
          documents: [LIVE_DOCUMENT],
          refreshedAt: "2026-07-22T00:00:00Z",
          error: null,
        },
        scratches: [
          {
            scratchId: "local-picker",
            title: "Browser draft",
            createdAt: "2026-07-25T12:00:00Z",
            updatedAt: "2026-07-25T12:05:00Z",
            recoveredFromPreviousEditor: false,
          },
        ],
        routeTarget: { kind: "launcher", storeId: LIVE_FOLDER.storeId },
        activeSession: { kind: "none" },
        openingTarget: null,
        navigationError: null,
        readOnly: false,
      },
      emit,
    );

    const openDocumentButton = screen.getByRole("button", { name: "Open document" });
    expect(openDocumentButton).not.toHaveTextContent("…");
    expect(openDocumentButton.querySelector("svg")).toBeNull();
    expect(screen.queryByRole("button", { name: "New document" })).toBeNull();
    expect(screen.getByRole("button", { name: "New" })).toBeVisible();
    expect(screen.getByRole("button", { name: "From file" })).toBeVisible();

    await user.click(openDocumentButton);

    expect(
      screen.getByRole("dialog", { name: "Open document" }),
    ).toBeVisible();
    expect(
      screen.getByRole("option", {
        name: /Live doc.*work-buddy.*docs\/live\.md/,
      }),
    ).toBeVisible();
    expect(screen.queryByRole("option", { name: /Browser draft/ })).toBeNull();
    expect(emit).not.toHaveBeenCalled();
  });

  it("keeps an active document in place when the native Folder picker fails", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: Parameters<typeof noopEmit>[0]) =>
      intent.intent_type === COWORK_INTENTS.folderSelect
        ? {
            intent_id: intent.intent_id,
            status: "rejected" as const,
            message: "The folder picker couldn’t be opened.",
          }
        : {
            intent_id: intent.intent_id,
            status: "accepted" as const,
          },
    );
    const { container } = renderLive(emit);

    await user.click(screen.getByRole("button", { name: "work-buddy" }));

    expect(
      screen.getAllByText("The folder picker couldn’t be opened."),
    ).toHaveLength(1);
    expect(container.querySelector(".wb-cowork-lifecycle__session")).not.toBeNull();
    expect(container.querySelector(".wb-cowork-lifecycle__session")).not.toHaveAttribute(
      "inert",
    );
  });

  it("lets a document return from failed Folder setup without duplicating the error", async () => {
    const user = userEvent.setup();
    const message = "Co-work couldn’t finish opening archive.";
    const emit = vi.fn(async (intent: Parameters<typeof noopEmit>[0]) => ({
      intent_id: intent.intent_id,
      status: "rejected" as const,
      message,
    }));
    globalThis.fetch = liveFetch() as unknown as typeof fetch;
    renderWorkspace(
      {
        document: LIVE_DOCUMENT,
        sessionQuality: "complete",
        folders: [LIVE_FOLDER],
        folderSelection: {
          kind: "setup_available",
          candidate: {
            folderName: "archive",
            folderPath: "C:/Projects/archive",
          },
        },
        activeFolderStoreId: LIVE_FOLDER.storeId,
        catalog: {
          status: "ready",
          documents: [LIVE_DOCUMENT],
          refreshedAt: "2026-07-22T00:00:00Z",
          error: null,
        },
        scratches: [],
        routeTarget: {
          kind: "registered",
          storeId: LIVE_FOLDER.storeId,
          documentId: LIVE_DOCUMENT.documentId,
        },
        activeSession: {
          kind: "registered",
          storeId: LIVE_FOLDER.storeId,
          document: LIVE_DOCUMENT,
        },
        openingTarget: null,
        navigationError: {
          code: "setup_failed",
          message,
          retryable: true,
        },
        readOnly: false,
      },
      emit,
    );

    expect(screen.getByRole("button", { name: "Back to document" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(screen.getAllByText(message)).toHaveLength(1);
    expect(emit).toHaveBeenLastCalledWith(
      expect.objectContaining({
        intent_type: COWORK_INTENTS.folderSelect,
        payload: { action: "initialize" },
      }),
    );
  });

  it("refreshes the open document on focus and coalesces a burst", async () => {
    let releaseRefresh!: () => void;
    const refreshGate = new Promise<void>((resolve) => {
      releaseRefresh = resolve;
    });
    const emit = vi.fn(async (intent: Parameters<typeof noopEmit>[0]) => {
      if (intent.intent_type === "wb.cowork.catalog.refresh") await refreshGate;
      return { intent_id: intent.intent_id, status: "accepted" as const };
    });
    renderLive(emit);
    emit.mockClear();

    act(() => {
      window.dispatchEvent(new Event("focus"));
      window.dispatchEvent(new Event("focus"));
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(emit).toHaveBeenCalledTimes(1);
    expect(emit.mock.calls[0]?.[0].intent_type).toBe("wb.cowork.catalog.refresh");

    releaseRefresh();
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(2));
    expect(
      emit.mock.calls.every(
        ([intent]) => intent.intent_type === "wb.cowork.catalog.refresh",
      ),
    ).toBe(true);
  });

  it("polls only while the open document is visible", async () => {
    vi.useFakeTimers();
    let visibility: DocumentVisibilityState = "hidden";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(
      () => visibility,
    );
    const emit = vi.fn(async (intent: Parameters<typeof noopEmit>[0]) => ({
      intent_id: intent.intent_id,
      status: "accepted" as const,
    }));
    renderLive(emit);
    emit.mockClear();

    await act(async () => {
      vi.advanceTimersByTime(60_000);
      await Promise.resolve();
    });
    expect(emit).not.toHaveBeenCalled();

    visibility = "visible";
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await Promise.resolve();
    });
    expect(emit).toHaveBeenCalledTimes(1);

    emit.mockClear();
    await act(async () => {
      vi.advanceTimersByTime(60_000);
      await Promise.resolve();
    });
    expect(emit).toHaveBeenCalledTimes(1);
  });

  it("fails closed when a registered document has no canonical structured snapshot", async () => {
    const { container } = renderLive();

    await waitFor(() => expect(screen.getByText("Name the review rail.")).toBeVisible(), {
      timeout: 10_000,
    });
    await waitFor(
      () => expect(screen.getByText("Document couldn’t be opened.")).toBeVisible(),
      { timeout: 10_000 },
    );
    expect(container.querySelector("[data-wb-suggestion]")).toBeNull();
    expect(screen.queryByText(/This is the editor pane/)).toBeNull();
  }, 15_000);

  it("uses mounted Editor, Review, Provenance, Truth, and Chat peer panes with roving focus on narrow screens", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: query === "(max-width: 760px)",
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(() => true),
      })),
    );
    const user = userEvent.setup();
    renderLive();

    const paneTabs = await screen.findByRole("tablist", { name: "Co-work panes" });
    const editorPanel = screen.getByTestId("editor");
    const railPanel = screen.getByTestId("rail");
    const split = editorPanel.parentElement;
    expect(split).toHaveClass("wb-workspace-side-panel");
    expect(split).toHaveAttribute("data-workspace-panel-mode", "primary-only");
    expect(railPanel).toHaveAttribute("hidden");
    expect(railPanel).toHaveAttribute("inert");
    expect(screen.queryByRole("separator")).not.toBeInTheDocument();
    const editorTab = within(paneTabs).getByRole("tab", { name: "Editor" });
    const reviewTab = within(paneTabs).getByRole("tab", { name: "Review" });
    const provenanceTab = within(paneTabs).getByRole("tab", { name: "Provenance" });
    const truthTab = within(paneTabs).getByRole("tab", { name: "Truth" });
    const chatTab = within(paneTabs).getByRole("tab", { name: "Chat" });
    expect(editorTab).toHaveAttribute("aria-selected", "true");
    expect(editorTab).toHaveAttribute("tabindex", "0");
    expect(reviewTab).toHaveAttribute("tabindex", "-1");
    expect(provenanceTab).toHaveAttribute("tabindex", "-1");
    expect(truthTab).toHaveAttribute("tabindex", "-1");
    expect(chatTab).toHaveAttribute("tabindex", "-1");
    expect(document.getElementById("wb-cowork-mobile-panel-editor")).toBeVisible();
    expect(document.getElementById("wb-cowork-rail-panel-review")).not.toBeVisible();

    editorTab.focus();
    await user.keyboard("{ArrowRight}");
    expect(reviewTab).toHaveFocus();
    expect(reviewTab).toHaveAttribute("aria-selected", "true");
    expect(reviewTab).toHaveAttribute("tabindex", "0");
    expect(editorTab).toHaveAttribute("tabindex", "-1");
    expect(screen.getByTestId("editor")).toBe(editorPanel);
    expect(screen.getByTestId("rail")).toBe(railPanel);
    expect(split).toHaveAttribute("data-workspace-panel-mode", "side-only");
    expect(editorPanel).toHaveAttribute("hidden");
    expect(editorPanel).toHaveAttribute("inert");
    expect(railPanel).not.toHaveAttribute("hidden");
    expect(document.getElementById("wb-cowork-mobile-panel-editor")).toHaveAttribute(
      "inert",
    );
    expect(document.getElementById("wb-cowork-mobile-panel-editor")).not.toBeVisible();
    expect(document.getElementById("wb-cowork-rail-panel-review")).toBeVisible();

    await waitFor(() => expect(screen.getByText("Name the review rail.")).toBeVisible(), {
      timeout: 10_000,
    });
    await user.click(screen.getByText("Name the review rail."));
    await waitFor(() =>
      expect(editorTab).toHaveAttribute("aria-selected", "true"),
    );
    // Review activation is cross-pane navigation on a narrow workspace. The
    // editor becomes visible before the bridge attempts the selected passage.

    await user.click(reviewTab);
    await user.click(screen.getByRole("button", { name: "Accept" }));
    expect(screen.getByText("Decision: Accept")).toBeVisible();

    await user.click(editorTab);
    expect(editorTab).toHaveAttribute("aria-selected", "true");
    await user.click(reviewTab);
    expect(screen.getByText("Decision: Accept")).toBeVisible();

    reviewTab.focus();
    await user.keyboard("{ArrowRight}");
    expect(provenanceTab).toHaveFocus();
    expect(provenanceTab).toHaveAttribute("aria-selected", "true");
    expect(provenanceTab).toHaveAttribute("tabindex", "0");
    expect(reviewTab).toHaveAttribute("tabindex", "-1");
    expect(document.getElementById("wb-cowork-rail-panel-provenance")).toBeVisible();
    expect(document.getElementById("wb-cowork-rail-panel-review")).not.toBeVisible();
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "Document provenance" })).toBeVisible(),
    );

    await user.keyboard("{ArrowRight}");
    expect(truthTab).toHaveFocus();
    expect(truthTab).toHaveAttribute("aria-selected", "true");
    expect(truthTab).toHaveAttribute("tabindex", "0");
    expect(reviewTab).toHaveAttribute("tabindex", "-1");
    expect(document.getElementById("wb-cowork-rail-panel-truth")).toBeVisible();
    expect(document.getElementById("wb-cowork-rail-panel-review")).not.toBeVisible();

    await user.keyboard("{End}");
    expect(chatTab).toHaveFocus();
    expect(chatTab).toHaveAttribute("aria-selected", "true");
  }, 15_000);
});

describe("resolveFixtureMode: demo is dev-only, empty and live are the product modes", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("resolves ?cowork_fixture=demo to the demo scene when DEV is true", () => {
    vi.stubEnv("DEV", true);
    expect(resolveFixtureMode("demo", "demo-doc", undefined, "demo")).toBe("demo");
  });

  it("falls back to the honest empty default for ?cowork_fixture=demo in a production build", () => {
    // import.meta.env.DEV is statically false in production, so the demo entry is scrapped and
    // the CoworkDemoWorkspace it would select is tree-shaken out.
    vi.stubEnv("DEV", false);
    expect(resolveFixtureMode("demo", "demo-doc", undefined, "demo")).toBe("empty");
  });

  it("resolves a store-scoped session to live regardless of the demo gate", () => {
    vi.stubEnv("DEV", false);
    expect(resolveFixtureMode("complete", "live-doc", "live-store", null)).toBe("live");
  });

  it("defaults to the honest empty state with no override and no store id", () => {
    expect(resolveFixtureMode("demo", undefined, undefined, null)).toBe("empty");
  });
});
