import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import {
  CoworkExportButton,
  coworkExportBlockedReason,
} from "./CoworkExportButton";

const formatsResponse = (formats: unknown[]) =>
  new Response(JSON.stringify({ ok: true, formats }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

const MARKDOWN_ONLY = [
  { format: "markdown", label: "Markdown", extension: ".md", media_type: "text/markdown" },
];

const WITH_PANDOC = [
  ...MARKDOWN_ONLY,
  { format: "latex", label: "LaTeX", extension: ".tex", media_type: "application/x-tex" },
  { format: "pdf", label: "PDF", extension: ".pdf", media_type: "application/pdf" },
];

beforeEach(() => {
  Object.defineProperty(URL, "createObjectURL", {
    value: vi.fn(() => "blob:stub"),
    configurable: true,
  });
  Object.defineProperty(URL, "revokeObjectURL", { value: vi.fn(), configurable: true });
});

describe("coworkExportBlockedReason", () => {
  it("permits export from a settled document", () => {
    expect(coworkExportBlockedReason("clean")).toBeNull();
  });

  it("asks the author to wait while the outbox drains", () => {
    expect(coworkExportBlockedReason("saving")).toMatch(/finish saving/iu);
    expect(coworkExportBlockedReason("retrying")).toMatch(/finish saving/iu);
  });

  it("refuses while unsynced, so no one receives a stale copy", () => {
    for (const status of ["offline", "error", "conflict", "saved_on_device"] as const) {
      expect(coworkExportBlockedReason(status)).not.toBeNull();
    }
  });
});

describe("CoworkExportButton", () => {
  it("offers only the formats the host can produce", async () => {
    const fetchImpl = vi.fn(async () => formatsResponse(MARKDOWN_ONLY));

    render(
      <CoworkExportButton
        storeId="store"
        documentId="doc"
        syncStatus="clean"
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );

    await waitFor(() => expect(fetchImpl).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "Export as Markdown" })).toBeInTheDocument();
    // No toolchain means no dropdown at all, rather than a menu of dead entries.
    expect(
      screen.queryByRole("button", { name: "Choose an export format" }),
    ).not.toBeInTheDocument();
  });

  it("discovers formats once, not once per render", async () => {
    // Regression: depending on a wrapper built during render re-ran the effect,
    // which set state, which rendered again. The loop did not just spin this
    // control, it stalled the surface around it so documents never opened.
    const fetchImpl = vi.fn(async () => formatsResponse(WITH_PANDOC));

    const { rerender } = render(
      <CoworkExportButton
        storeId="store"
        documentId="doc"
        syncStatus="clean"
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    await screen.findByRole("button", { name: "Choose an export format" });

    for (const status of ["clean", "saving", "clean"] as const) {
      rerender(
        <CoworkExportButton
          storeId="store"
          documentId="doc"
          syncStatus={status}
          fetchImpl={fetchImpl as unknown as typeof fetch}
        />,
      );
    }

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("exposes the extra formats when the host has the toolchain", async () => {
    const fetchImpl = vi.fn(async () => formatsResponse(WITH_PANDOC));

    render(
      <CoworkExportButton
        storeId="store"
        documentId="doc"
        syncStatus="clean"
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );

    const trigger = await screen.findByRole("button", { name: "Choose an export format" });
    await userEvent.click(trigger);

    expect(await screen.findByRole("menuitem", { name: "LaTeX" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "PDF" })).toBeInTheDocument();
    // Markdown is the primary click, so repeating it in the menu would be noise.
    expect(screen.queryByRole("menuitem", { name: "Markdown" })).not.toBeInTheDocument();
  });

  it("requests the chosen format for the given document", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/render/formats")) return formatsResponse(MARKDOWN_ONLY);
      return new Response("# Doc\n", {
        status: 200,
        headers: { "Content-Disposition": 'attachment; filename="doc-abc.md"' },
      });
    });

    render(
      <CoworkExportButton
        storeId="store-1"
        documentId="doc-1"
        syncStatus="clean"
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Export as Markdown" }));

    await waitFor(() => {
      const requested = fetchImpl.mock.calls.map((call) => String(call[0]));
      expect(
        requested.some(
          (url) =>
            url.includes("/api/truth/doc/doc-1/render") &&
            url.includes("store_id=store-1") &&
            url.includes("format=markdown"),
        ),
      ).toBe(true);
    });
  });

  it("surfaces a refusal instead of failing silently", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/render/formats")) return formatsResponse(MARKDOWN_ONLY);
      return new Response(
        JSON.stringify({ ok: false, error: { code: "stale_head", message: "The document moved on." } }),
        { status: 409, headers: { "content-type": "application/json" } },
      );
    });

    render(
      <CoworkExportButton
        storeId="store"
        documentId="doc"
        syncStatus="clean"
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Export as Markdown" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The document moved on.");
  });

  it("blocks with a reason the assistive tree can reach", async () => {
    const fetchImpl = vi.fn(async () => formatsResponse(MARKDOWN_ONLY));

    const { container } = render(
      <CoworkExportButton
        storeId="store"
        documentId="doc"
        syncStatus="offline"
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );

    const button = screen.getByRole("button", { name: "Export as Markdown" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-describedby", "cowork-export-blocked-reason");
    expect(container.querySelector("#cowork-export-blocked-reason")).toHaveTextContent(
      /sync this document/iu,
    );
  });

  it("has no accessibility violations in either state", async () => {
    const fetchImpl = vi.fn(async () => formatsResponse(WITH_PANDOC));

    const ready = render(
      <CoworkExportButton
        storeId="store"
        documentId="doc"
        syncStatus="clean"
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    await screen.findByRole("button", { name: "Choose an export format" });
    await expectNoAccessibilityViolations(ready.container);
    ready.unmount();

    const blocked = render(
      <CoworkExportButton
        storeId="store"
        documentId="doc"
        syncStatus="conflict"
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    await expectNoAccessibilityViolations(blocked.container);
  });
});
