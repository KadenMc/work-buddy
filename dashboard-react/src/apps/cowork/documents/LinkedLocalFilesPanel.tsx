import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FilePdf, FolderOpen, Key } from "@phosphor-icons/react";

import { Button, InlineAlert } from "../../../ui";
import {
  HttpCoworkLocalFileClient,
  linkedLocalFileWarning,
  type CoworkLocalFileClient,
  type CoworkLocalFileLink,
} from "../localFiles";
import "./linked-local-files.css";

interface LinkedLocalFilesPanelProps {
  readonly storeId: string;
  readonly documentId: string;
  readonly client?: CoworkLocalFileClient;
  readonly confirmCredentialReveal?: (warning: string) => boolean;
}

const byteLabel = (value: number): string => {
  if (value < 1024) return `${String(value)} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
};

const availabilityLabel = (link: CoworkLocalFileLink): string => {
  if (link.availability === "verified") {
    return link.localActionAvailable ? "Verified locally" : "Available on the host only";
  }
  if (link.availability === "changed") return "File changed — relink required";
  if (link.availability === "policy_changed") return "Revalidation required";
  return "File unavailable";
};

export function LinkedLocalFilesPanel({
  storeId,
  documentId,
  client,
  confirmCredentialReveal = (warning) => globalThis.confirm(warning),
}: LinkedLocalFilesPanelProps) {
  const resolvedClient = useMemo<CoworkLocalFileClient>(
    () => client ?? new HttpCoworkLocalFileClient({ storeId, documentId }),
    [client, documentId, storeId],
  );
  const [links, setLinks] = useState<readonly CoworkLocalFileLink[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyLinkId, setBusyLinkId] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const refreshGeneration = useRef(0);
  const documentIdentity = `${storeId}\u0000${documentId}`;
  const documentIdentityRef = useRef(documentIdentity);
  documentIdentityRef.current = documentIdentity;

  const refresh = useCallback(async (
    recheck = false,
    announce = false,
  ): Promise<boolean> => {
    const generation = ++refreshGeneration.current;
    const requestedIdentity = documentIdentity;
    setRefreshing(true);
    if (announce) setStatus(null);
    try {
      const result = await resolvedClient.list({ refresh: recheck });
      if (
        generation !== refreshGeneration.current ||
        requestedIdentity !== documentIdentityRef.current
      ) {
        return false;
      }
      setLinks(result);
      setError(null);
      if (announce) setStatus("Linked-file availability was rechecked.");
      return true;
    } catch {
      if (
        generation !== refreshGeneration.current ||
        requestedIdentity !== documentIdentityRef.current
      ) {
        return false;
      }
      setLinks((current) => current ?? []);
      setError("Co-work couldn’t inspect the linked local files.");
      return false;
    } finally {
      if (
        generation === refreshGeneration.current &&
        requestedIdentity === documentIdentityRef.current
      ) {
        setRefreshing(false);
      }
    }
  }, [documentIdentity, resolvedClient]);

  useEffect(() => {
    refreshGeneration.current += 1;
    setLinks(null);
    setError(null);
    setRefreshing(false);
    setStatus(null);
    void refresh();
    return () => {
      refreshGeneration.current += 1;
    };
  }, [refresh]);

  const activate = useCallback(
    async (link: CoworkLocalFileLink): Promise<void> => {
      const warning = linkedLocalFileWarning(link);
      if (warning && !confirmCredentialReveal(warning)) return;
      setBusyLinkId(link.linkId);
      setError(null);
      setStatus(null);
      try {
        await resolvedClient.activate(link);
        setStatus(
          link.allowedAction === "open"
            ? `${link.displayName} was opened locally.`
            : `The location of ${link.displayName} was revealed.`,
        );
      } catch {
        const rechecked = await refresh(true);
        if (rechecked) {
          setError(
            "The linked local file could not be opened. Its bytes remain untouched, and its availability was rechecked.",
          );
        }
      } finally {
        setBusyLinkId(null);
      }
    },
    [confirmCredentialReveal, refresh, resolvedClient],
  );

  if (links === null || (links.length === 0 && error === null)) return null;

  return (
    <section
      className="wb-cowork__linked-files"
      aria-label="Linked local files"
    >
      <details>
        <summary>
          Linked local files{links.length > 0 ? ` (${String(links.length)})` : ""}
        </summary>
        {error !== null ? (
          <InlineAlert tone="warning" role="alert">
            <span>{error}</span>
            <Button size="small" onClick={() => void refresh(true, true)}>
              Try again
            </Button>
          </InlineAlert>
        ) : null}
        {links.length > 0 ? (
          <>
            <div className="wb-cowork__linked-file-controls">
              <Button
                size="small"
                variant="ghost"
                disabled={refreshing || busyLinkId !== null}
                onClick={() => void refresh(true, true)}
              >
                {refreshing ? "Rechecking…" : "Recheck availability"}
              </Button>
            </div>
            <ul className="wb-cowork__linked-file-list">
              {links.map((link) => {
                const sensitive = link.allowedAction === "reveal";
                const disabled =
                  busyLinkId !== null ||
                  refreshing ||
                  link.availability !== "verified" ||
                  !link.localActionAvailable;
                return (
                  <li key={link.linkId} className="wb-cowork__linked-file">
                    <span className="wb-cowork__linked-file-icon" aria-hidden="true">
                      {sensitive ? <Key weight="duotone" /> : <FilePdf weight="duotone" />}
                    </span>
                    <span className="wb-cowork__linked-file-copy">
                      <strong>{link.displayName}</strong>
                      <small>
                        {link.suffix.slice(1).toUpperCase()} · {byteLabel(link.byteLength)} ·{" "}
                        {availabilityLabel(link)}
                      </small>
                      {sensitive ? (
                        <small className="wb-cowork__linked-file-warning">
                          Credential-like file — reveal location only
                        </small>
                      ) : null}
                    </span>
                    <Button
                      size="small"
                      variant="ghost"
                      disabled={disabled}
                      title={
                        disabled
                          ? availabilityLabel(link)
                          : sensitive
                            ? "Reveal the file location without opening it."
                            : "Open the verified PDF with this computer."
                      }
                      onClick={() => void activate(link)}
                    >
                      {sensitive ? <FolderOpen aria-hidden="true" /> : <FilePdf aria-hidden="true" />}
                      {busyLinkId === link.linkId
                        ? "Working…"
                        : sensitive
                          ? "Reveal location"
                          : "Open locally"}
                    </Button>
                  </li>
                );
              })}
            </ul>
          </>
        ) : null}
        {status !== null ? (
          <p className="wb-cowork__linked-file-status" role="status">
            {status}
          </p>
        ) : null}
      </details>
    </section>
  );
}

export default LinkedLocalFilesPanel;
