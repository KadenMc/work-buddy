import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CoworkFolderSelection, CoworkViewModel } from "../contracts";
import { CoworkLauncher } from "./CoworkLauncher";

const candidate = {
  folderName: "work-buddy",
  folderPath: "C:/Projects/work-buddy",
};

const baseModel: CoworkViewModel = {
  folders: [],
  folderChooser: {
    available: true,
    kind: "host",
    importAvailable: true,
    locationAvailable: true,
  },
  folderSelection: { kind: "none" },
  activeFolderStoreId: null,
  catalog: { status: "ready", documents: [], refreshedAt: null, error: null },
  scratches: [],
  routeTarget: { kind: "launcher", storeId: null },
  activeSession: { kind: "none" },
  openingTarget: null,
  navigationError: null,
  readOnly: false,
  document: null,
};

const renderLauncher = (
  folderSelection: CoworkFolderSelection,
  overrides: Partial<CoworkViewModel> = {},
) => {
  const handlers = {
    onRetryInspection: vi.fn(),
    onCancelInspection: vi.fn(),
    onChooseFolder: vi.fn(),
    onInitialize: vi.fn(),
    onOpenFolder: vi.fn(),
    onOpenDocument: vi.fn(),
    onOpenLocalDocument: vi.fn(),
  };
  render(
    <CoworkLauncher
      model={{ ...baseModel, ...overrides, folderSelection }}
      {...handlers}
    />,
  );
  return handlers;
};

describe("CoworkLauncher descendant scan", () => {
  it("reports how many items the scan has checked", () => {
    renderLauncher({
      kind: "inspecting_descendants",
      candidate,
      progress: { visited: 12_450, complete: false },
    });

    expect(
      screen.getByText(`${(12_450).toLocaleString()} items checked`),
    ).toBeInTheDocument();
    expect(screen.getByText("Opening work-buddy…")).toBeInTheDocument();
  });

  it("cancels the scan from the busy state", async () => {
    const user = userEvent.setup();
    const { onCancelInspection } = renderLauncher({
      kind: "inspecting_descendants",
      candidate,
      progress: { visited: 12_450, complete: false },
    });

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancelInspection).toHaveBeenCalledTimes(1);
  });

  it("omits the count until the scan has checked something", () => {
    renderLauncher({
      kind: "inspecting_descendants",
      candidate,
      progress: { visited: 0, complete: false },
    });

    expect(screen.getByText("Opening work-buddy…")).toBeInTheDocument();
    expect(screen.queryByText(/items checked/)).toBeNull();
  });
});

describe("CoworkLauncher folder refusal", () => {
  it("offers a way out of an oversized folder with no session and no active folder", async () => {
    const user = userEvent.setup();
    const { onChooseFolder } = renderLauncher({
      kind: "unavailable",
      candidate,
      reasonCode: "folder_too_large_for_safe_setup",
      retryable: false,
      availableActions: ["choose_narrower_folder"],
    });

    expect(
      screen.getByText("That folder holds too many items for Co-work to check safely."),
    ).toBeInTheDocument();
    const chooser = screen.getByRole("button", { name: "Choose a smaller folder" });
    expect(screen.getAllByRole("button")).toHaveLength(1);

    await user.click(chooser);

    expect(onChooseFolder).toHaveBeenCalledTimes(1);
  });

  it("holds the chooser back where the host cannot pick a folder", () => {
    renderLauncher(
      {
        kind: "unavailable",
        candidate,
        reasonCode: "folder_too_large_for_safe_setup",
        retryable: false,
        availableActions: ["choose_narrower_folder"],
      },
      {
        folderChooser: {
          available: false,
          kind: "none",
          importAvailable: false,
          locationAvailable: false,
        },
      },
    );

    expect(screen.queryByRole("button", { name: /Choose/ })).toBeNull();
  });

  it("keeps Try again for a refusal the user can retry", async () => {
    const user = userEvent.setup();
    const { onRetryInspection } = renderLauncher({
      kind: "unavailable",
      candidate,
      reasonCode: "descendant_scan_incomplete",
      retryable: true,
      availableActions: ["retry"],
    });

    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(onRetryInspection).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: /Choose/ })).toBeNull();
  });
});
