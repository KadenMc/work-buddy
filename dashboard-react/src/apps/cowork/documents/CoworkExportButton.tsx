import { useEffect, useState } from "react";
import { CaretDown } from "@phosphor-icons/react/CaretDown";
import { DownloadSimple } from "@phosphor-icons/react/DownloadSimple";
import { Menu, MenuItem, MenuTrigger, Popover, type Key } from "react-aria-components";

import { Button } from "../../../ui";
import type { CoworkSyncStatus } from "../persistence/CoworkYdocPersistence";

export interface CoworkRenderFormat {
  readonly format: string;
  readonly label: string;
  readonly extension: string;
  readonly media_type: string;
}

interface CoworkExportButtonProps {
  readonly storeId: string;
  readonly documentId: string;
  readonly syncStatus?: CoworkSyncStatus;
  readonly disabled?: boolean;
  readonly fetchImpl?: typeof fetch;
}

/**
 * Why export is gated on a settled document rather than always available.
 *
 * Structured edits reach the server through an outbox, so a document can be
 * complete on screen and not yet complete in the store. An export taken during
 * that window would be a file the author believes is current and is not, with
 * nothing in the artifact to reveal the difference. Waiting is cheap; an
 * unnoticed stale copy sent to someone else is not.
 */
export const coworkExportBlockedReason = (
  syncStatus?: CoworkSyncStatus,
): string | null => {
  if (syncStatus === undefined) return null;
  if (syncStatus === "clean") return null;
  if (syncStatus === "saving" || syncStatus === "retrying") {
    return "Wait for Co-work to finish saving before exporting.";
  }
  return "Sync this document to Co-work before exporting.";
};

const MARKDOWN: CoworkRenderFormat = {
  format: "markdown",
  label: "Markdown",
  extension: ".md",
  media_type: "text/markdown; charset=utf-8",
};

const filenameFrom = (disposition: string | null, fallback: string): string => {
  const match = disposition?.match(/filename="([^"]+)"/u);
  return match?.[1] ?? fallback;
};

export function CoworkExportButton({
  storeId,
  documentId,
  syncStatus,
  disabled = false,
  fetchImpl,
}: CoworkExportButtonProps) {
  const [formats, setFormats] = useState<readonly CoworkRenderFormat[]>([MARKDOWN]);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  // Depend on the injected implementation itself, never on a wrapper created
  // during render. A fresh closure each render would re-run this effect, which
  // sets state, which renders again: an unbounded loop that stalls the whole
  // Co-work surface rather than just this control.
  useEffect(() => {
    const request = fetchImpl ?? fetch;
    let cancelled = false;
    void (async () => {
      try {
        const response = await request("/api/truth/cowork/render/formats", {
          headers: { accept: "application/json" },
        });
        if (!response.ok) return;
        const payload = (await response.json()) as { formats?: CoworkRenderFormat[] };
        // A host without pandoc offers fewer formats. Showing one it cannot
        // produce would be a menu entry whose only outcome is an error.
        if (!cancelled && payload.formats && payload.formats.length > 0) {
          setFormats(payload.formats);
        }
      } catch {
        // Markdown needs no toolchain, so the default list stays correct.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [fetchImpl]);

  const blockedReason = coworkExportBlockedReason(syncStatus);
  const unavailable = disabled || busy || blockedReason !== null;

  const exportAs = async (format: string): Promise<void> => {
    const request = fetchImpl ?? fetch;
    setBusy(true);
    setFailure(null);
    try {
      const query = new URLSearchParams({ store_id: storeId, format });
      const response = await request(
        `/api/truth/doc/${encodeURIComponent(documentId)}/render?${query.toString()}`,
      );
      if (!response.ok) {
        const payload = (await response.json().catch(() => null)) as
          | { error?: { message?: string } }
          | null;
        setFailure(payload?.error?.message ?? "Co-work could not export this document.");
        return;
      }
      const blob = await response.blob();
      const href = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.download = filenameFrom(
        response.headers.get("Content-Disposition"),
        `document${format === "markdown" ? ".md" : ""}`,
      );
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(href);
    } catch {
      setFailure("Co-work could not export this document.");
    } finally {
      setBusy(false);
    }
  };

  const alternates = formats.filter((entry) => entry.format !== "markdown");

  return (
    <span className="wb-cowork__export">
      <Button
        size="small"
        variant="ghost"
        onClick={() => void exportAs("markdown")}
        disabled={unavailable}
        aria-label="Export as Markdown"
        title={blockedReason ?? "Export as Markdown"}
        aria-describedby={blockedReason !== null ? "cowork-export-blocked-reason" : undefined}
      >
        <DownloadSimple aria-hidden="true" /> {busy ? "Exporting…" : "Export"}
      </Button>
      {alternates.length > 0 ? (
        <MenuTrigger>
          <Button
            size="small"
            variant="ghost"
            disabled={unavailable}
            aria-label="Choose an export format"
            title="Choose an export format"
          >
            <CaretDown weight="bold" aria-hidden="true" />
          </Button>
          <Popover className="wb-popover" placement="bottom end">
            <Menu
              aria-label="Export format"
              className="wb-action-menu"
              onAction={(key: Key) => void exportAs(String(key))}
            >
              {alternates.map((entry) => (
                <MenuItem
                  key={entry.format}
                  id={entry.format}
                  className="wb-action-menu__item"
                  textValue={entry.label}
                >
                  {entry.label}
                </MenuItem>
              ))}
            </Menu>
          </Popover>
        </MenuTrigger>
      ) : null}
      {blockedReason !== null ? (
        <span id="cowork-export-blocked-reason" className="wb-visually-hidden">
          {blockedReason}
        </span>
      ) : null}
      {failure !== null ? (
        <span className="wb-cowork__save-message is-error" role="alert">
          {failure}
        </span>
      ) : null}
    </span>
  );
}
