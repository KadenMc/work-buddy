import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  asViewId,
  asWidgetInstanceId,
  type WidgetPresentationContext,
} from "../../../dashboard/contributions/contracts";
import { DashboardEventProvider } from "../../../dashboard/events/DashboardEventProvider";
import { fallbackCanvasTheme } from "../../../theme/resolveTheme";
import { expectNoAccessibilityViolations } from "../../../test/setup";
import {
  COWORK_INTENTS,
  type CoworkDocumentSummary,
  type CoworkWorkspaceInput,
} from "../contracts";
import { saveRailTab } from "../guards";
import CoworkWorkspaceWidget, {
  reimportReceiptMatchesDocument,
} from "../widget/CoworkWorkspaceWidget";
import { resolveFixtureMode } from "./CoworkWorkspaceSurface";

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

describe("CoworkWorkspaceWidget default (empty) mode", () => {
  const originalUrl = window.location.href;
  beforeEach(() => window.history.replaceState({}, "", "/app/cowork"));
  afterEach(() => window.history.replaceState({}, "", originalUrl));

  const emptyInput: CoworkWorkspaceInput = {
    document: null,
    sessionQuality: "demo",
  };

  it("opens with direct Folder selection and the stable toolbar actions", async () => {
    const { container } = renderWorkspace(emptyInput);

    expect(screen.getByRole("button", { name: "Open folder" })).toBeVisible();
    expect(screen.getByRole("button", { name: "New" })).toBeVisible();
    expect(
      screen.getByRole("button", { name: "New from Markdown" }),
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
  driftState: "clean",
  openProposalCount: 0,
  openFlagCount: 0,
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

/** Route the live surface's direct route calls: R2 read, R3 ydoc pull, R4 ydoc push. */
const liveFetch = () =>
  vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
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
    saveRailTab(window.localStorage, "live-doc", "review");
  });

  it("restores a persisted Chat view with GET only", async () => {
    saveRailTab(window.localStorage, "live-doc", "chat");
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
    saveRailTab(window.localStorage, "live-doc", "review");
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
    saveRailTab(window.localStorage, "live-doc", "chat");
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
    saveRailTab(window.localStorage, "live-doc", "review");
  });

  it("ensures on a current Chat click, then loads only the returned opaque id", async () => {
    const baseFetch = liveFetch();
    const ensuredId = "server-issued-after-click-72";
    const fetchImpl = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        const url = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        if (url.includes("/api/truth/doc/live-doc/conversation")) {
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
          return jsonResponse({
            ok: true,
            conversation_id: ensuredId,
            created: true,
            agent: { ...LIVE_AGENT, started: true },
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
          String(input).includes("/conversation?store_id=live-store") &&
          init?.method === "POST",
      ),
    ).toBe(true);
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
    saveRailTab(window.localStorage, "live-doc", "review");
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
      within(launcher).queryByRole("button", { name: "New from Markdown" }),
    ).toBeNull();
    expect(screen.getByRole("button", { name: "New" })).toBeVisible();
    expect(screen.getByRole("button", { name: "New from Markdown" })).toBeVisible();

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

  it("keeps New from Markdown unavailable when the host cannot open that picker", async () => {
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
          markdownAvailable: false,
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

    const fromMarkdown = screen.getByRole("button", {
      name: "New from Markdown",
    });
    expect(fromMarkdown).toHaveAttribute("aria-disabled", "true");
    expect(fromMarkdown).toBeEnabled();
    fromMarkdown.focus();
    expect(fromMarkdown).toHaveFocus();
    expect(fromMarkdown).toHaveAccessibleDescription(
      "Markdown file selection isn’t available here.",
    );
    expect(
      screen.getByText("Markdown file selection isn’t available here."),
    ).toBeVisible();

    await user.click(fromMarkdown);
    expect(
      screen.queryByRole("dialog", { name: "New document from Markdown" }),
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
    expect(screen.getByRole("button", { name: "New from Markdown" })).toBeVisible();

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

  it("uses mounted Editor, Review, and Chat peer panes with roving focus on narrow screens", async () => {
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
    const editorTab = within(paneTabs).getByRole("tab", { name: "Editor" });
    const reviewTab = within(paneTabs).getByRole("tab", { name: "Review" });
    const chatTab = within(paneTabs).getByRole("tab", { name: "Chat" });
    expect(editorTab).toHaveAttribute("aria-selected", "true");
    expect(editorTab).toHaveAttribute("tabindex", "0");
    expect(reviewTab).toHaveAttribute("tabindex", "-1");
    expect(chatTab).toHaveAttribute("tabindex", "-1");
    expect(document.getElementById("wb-cowork-mobile-panel-editor")).toBeVisible();
    expect(document.getElementById("wb-cowork-rail-panel-review")).not.toBeVisible();

    editorTab.focus();
    await user.keyboard("{ArrowRight}");
    expect(reviewTab).toHaveFocus();
    expect(reviewTab).toHaveAttribute("aria-selected", "true");
    expect(reviewTab).toHaveAttribute("tabindex", "0");
    expect(editorTab).toHaveAttribute("tabindex", "-1");
    expect(document.getElementById("wb-cowork-mobile-panel-editor")).toHaveAttribute(
      "inert",
    );
    expect(document.getElementById("wb-cowork-mobile-panel-editor")).not.toBeVisible();
    expect(document.getElementById("wb-cowork-rail-panel-review")).toBeVisible();

    await waitFor(() => expect(screen.getByText("Name the review rail.")).toBeVisible(), {
      timeout: 10_000,
    });
    await user.click(screen.getByText("Name the review rail."));
    await user.click(screen.getByRole("button", { name: "Accept" }));
    expect(screen.getByText("Decision: Accept")).toBeVisible();

    await user.click(editorTab);
    expect(editorTab).toHaveAttribute("aria-selected", "true");
    await user.click(reviewTab);
    expect(screen.getByText("Decision: Accept")).toBeVisible();

    reviewTab.focus();
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
