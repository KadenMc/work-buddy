import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  CoworkLocalFileClient,
  CoworkLocalFileLink,
} from "../localFiles";
import { LinkedLocalFilesPanel } from "./LinkedLocalFilesPanel";

const PDF_ID = `pdf_${"a".repeat(28)}`;
const PPK_ID = `ppk_${"b".repeat(28)}`;

const pdf: CoworkLocalFileLink = {
  linkId: PDF_ID,
  href: `wb-local-file:${PDF_ID}`,
  displayName: "Reference PDF",
  suffix: ".pdf",
  mediaType: "application/pdf",
  byteLength: 2048,
  sensitivity: "ordinary",
  allowedAction: "open",
  availability: "verified",
  localActionAvailable: true,
};

const ppk: CoworkLocalFileLink = {
  linkId: PPK_ID,
  href: `wb-local-file:${PPK_ID}`,
  displayName: "Credential key",
  suffix: ".ppk",
  mediaType: "application/x-putty-private-key",
  byteLength: 512,
  sensitivity: "credential",
  allowedAction: "reveal",
  availability: "verified",
  localActionAvailable: true,
};

describe("LinkedLocalFilesPanel", () => {
  it("shows metadata only and requires an explicit credential reveal warning", async () => {
    const user = userEvent.setup();
    const activate = vi.fn<CoworkLocalFileClient["activate"]>(async () => undefined);
    const client: CoworkLocalFileClient = {
      list: async () => [pdf, ppk],
      activate,
    };
    const confirmCredentialReveal = vi.fn(() => false);
    render(
      <LinkedLocalFilesPanel
        storeId={"s".repeat(32)}
        documentId={"d".repeat(32)}
        client={client}
        confirmCredentialReveal={confirmCredentialReveal}
      />,
    );

    const summary = await screen.findByText("Linked local files (2)");
    expect(summary).toBeVisible();
    await user.click(summary);
    expect(screen.getByText("Reference PDF")).toBeVisible();
    expect(screen.getByText("Credential key")).toBeVisible();
    expect(screen.queryByText(/C:\\/)).not.toBeInTheDocument();
    expect(screen.getByText(/Credential-like file/)).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Reveal location" }));
    expect(confirmCredentialReveal).toHaveBeenCalledWith(
      expect.stringContaining("will only reveal its location"),
    );
    expect(activate).not.toHaveBeenCalled();

    confirmCredentialReveal.mockReturnValue(true);
    await user.click(screen.getByRole("button", { name: "Reveal location" }));
    await waitFor(() => expect(activate).toHaveBeenCalledWith(ppk));
    expect(screen.getByRole("status")).toHaveTextContent(
      "The location of Credential key was revealed.",
    );
  });

  it("keeps host actions disabled for remote or changed metadata", async () => {
    const user = userEvent.setup();
    const client: CoworkLocalFileClient = {
      list: async () => [
        { ...pdf, localActionAvailable: false },
        { ...ppk, availability: "changed" },
      ],
      activate: async () => undefined,
    };
    render(
      <LinkedLocalFilesPanel
        storeId={"s".repeat(32)}
        documentId={"d".repeat(32)}
        client={client}
      />,
    );
    const summary = await screen.findByText("Linked local files (2)");
    expect(summary).toBeVisible();
    await user.click(summary);
    expect(screen.getByRole("button", { name: "Open locally" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reveal location" })).toBeDisabled();
    expect(screen.getByText(/File changed — relink required/)).toBeVisible();
  });

  it("rechecks unavailable links and replaces the captured catalog without rendering paths", async () => {
    const user = userEvent.setup();
    const unavailable = {
      ...pdf,
      availability: "unavailable" as const,
      localActionAvailable: false,
    };
    const restored = {
      ...pdf,
      // The public client contract ignores path-shaped extras. Keep one in the
      // test double to prove that the panel only renders admitted metadata.
      relative_path: "private/never-render-this.pdf",
    } as CoworkLocalFileLink;
    const list = vi.fn<CoworkLocalFileClient["list"]>(async (options) =>
      options?.refresh ? [restored] : [unavailable],
    );
    render(
      <LinkedLocalFilesPanel
        storeId={"s".repeat(32)}
        documentId={"d".repeat(32)}
        client={{ list, activate: async () => undefined }}
      />,
    );

    await user.click(await screen.findByText("Linked local files (1)"));
    expect(screen.getByRole("button", { name: "Open locally" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Recheck availability" }));

    await waitFor(() => {
      expect(list).toHaveBeenLastCalledWith({ refresh: true });
      expect(screen.getByRole("button", { name: "Open locally" })).toBeEnabled();
    });
    expect(screen.getByRole("status")).toHaveTextContent(
      "Linked-file availability was rechecked.",
    );
    expect(document.body).not.toHaveTextContent("private/never-render-this.pdf");
  });
});
