import { useEffect, useMemo, useRef, useState } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { Button, InlineAlert, Spinner } from "../../../ui";
import type { CoworkDocumentSummary } from "../contracts";
import {
  CoworkHttpClient,
  type CoworkDriftInspection,
  type CoworkReimportPrepared,
  type CoworkReimportReceipt,
} from "../providers/CoworkHttpClient";
import { asCoworkApiError } from "../providers/errors";
import { sha256Hex } from "../persistence/hashing";
import { bootstrapCoworkYdoc } from "./bootstrapCoworkYdoc";

const makeIdempotencyKey = (): string =>
  globalThis.crypto?.randomUUID?.() ??
  `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;

const decodePreview = (bytes: Uint8Array): string =>
  new TextDecoder("utf-8", { fatal: true }).decode(bytes).replace(/^\uFEFF/u, "");

const redline = (before: string, after: string): string => {
  const left = before.split(/\r\n|\r|\n/u);
  const right = after.split(/\r\n|\r|\n/u);
  let prefix = 0;
  while (prefix < left.length && prefix < right.length && left[prefix] === right[prefix]) {
    prefix += 1;
  }
  let suffix = 0;
  while (
    suffix < left.length - prefix &&
    suffix < right.length - prefix &&
    left[left.length - 1 - suffix] === right[right.length - 1 - suffix]
  ) {
    suffix += 1;
  }
  const contextStart = Math.max(0, prefix - 2);
  const beforeEnd = left.length - suffix;
  const afterEnd = right.length - suffix;
  const lines = [
    ...left.slice(contextStart, prefix).map((line) => `  ${line}`),
    ...left.slice(prefix, beforeEnd).map((line) => `- ${line}`),
    ...right.slice(prefix, afterEnd).map((line) => `+ ${line}`),
    ...right.slice(afterEnd, Math.min(right.length, afterEnd + 2)).map((line) => `  ${line}`),
  ];
  return lines.join("\n").slice(0, 12_000);
};

interface ReimportCommitPayload {
  readonly snapshot: Uint8Array;
  readonly snapshotSha256: string;
}

export interface CoworkReimportDialogProps {
  readonly storeId: string;
  readonly document: CoworkDocumentSummary;
  readonly client: CoworkHttpClient;
  readonly localBlockedReason?: string | null;
  readonly onClose: () => void;
  readonly onReimported: (receipt: CoworkReimportReceipt) => Promise<void> | void;
}

export function CoworkReimportDialog({
  storeId,
  document,
  client,
  localBlockedReason = null,
  onClose,
  onReimported,
}: CoworkReimportDialogProps) {
  const [drift, setDrift] = useState<CoworkDriftInspection | null>(null);
  const [comparison, setComparison] = useState<string>("");
  const [stage, setStage] = useState<
    "loading" | "review" | "preparing" | "confirm" | "committing"
  >("loading");
  const [error, setError] = useState<string | null>(null);
  const keyRef = useRef(makeIdempotencyKey());
  const preparedRef = useRef<CoworkReimportPrepared | null>(null);
  // Once the server commits, retries are local reconciliation only. Never replay or silently
  // no-op the destructive commit because closing/reopening the editor failed afterward.
  const committedRef = useRef<CoworkReimportReceipt | null>(null);
  // Preserve the exact prepared replacement across an ambiguous network result. The server
  // deletes staged source bytes after commit, so retry must replay the idempotent commit
  // directly instead of trying to read a source that a successful first attempt consumed.
  const commitPayloadRef = useRef<ReimportCommitPayload | null>(null);
  const busy = stage === "loading" || stage === "preparing" || stage === "committing";

  useEffect(() => {
    let active = true;
    void client
      .inspectDrift(storeId, document.documentId)
      .then(async (inspection) => {
        let rendered = "";
        if (
          inspection.diffAvailable &&
          inspection.baseline.available &&
          inspection.source.available
        ) {
          const [baseline, current] = await Promise.all([
            client.readDriftSource(inspection.baseline),
            client.readDriftSource(inspection.source),
          ]);
          if (
            inspection.baseline.sha256 !== null &&
            (await sha256Hex(baseline.bytes)) !== inspection.baseline.sha256
          ) {
            throw new Error("The saved Markdown baseline failed verification.");
          }
          if (
            inspection.source.sha256 !== null &&
            (await sha256Hex(current.bytes)) !== inspection.source.sha256
          ) {
            throw new Error("The external Markdown failed verification.");
          }
          rendered = redline(decodePreview(baseline.bytes), decodePreview(current.bytes));
        }
        if (!active) return;
        setDrift(inspection);
        setComparison(rendered);
        setStage("review");
      })
      .catch((loadError) => {
        if (!active) return;
        setError(asCoworkApiError(loadError).message);
        setStage("review");
      });
    return () => {
      active = false;
    };
  }, [client, document.documentId, storeId]);

  const close = (): void => {
    const prepared = preparedRef.current;
    if (
      committedRef.current === null &&
      prepared !== null &&
      prepared.state === "prepared"
    ) {
      void client
        .cancelReimport(storeId, document.documentId, prepared.intentId)
        .catch(() => undefined);
    }
    onClose();
  };

  const prepare = async (): Promise<void> => {
    if (localBlockedReason !== null) return;
    setError(null);
    setStage("preparing");
    try {
      const prepared = await client.prepareReimport(
        storeId,
        document.documentId,
        keyRef.current,
      );
      preparedRef.current = prepared;
      if (prepared.state === "committed" && prepared.result !== null) {
        committedRef.current = prepared.result;
        await onReimported(prepared.result);
        committedRef.current = null;
        preparedRef.current = null;
        return;
      }
      setStage("confirm");
    } catch (prepareError) {
      setError(asCoworkApiError(prepareError).message);
      setStage(committedRef.current === null ? "review" : "confirm");
    }
  };

  const commit = async (): Promise<void> => {
    const prepared = preparedRef.current;
    if (prepared === null || localBlockedReason !== null) return;
    setError(null);
    setStage("committing");
    try {
      if (committedRef.current !== null) {
        await onReimported(committedRef.current);
        committedRef.current = null;
        preparedRef.current = null;
        commitPayloadRef.current = null;
        return;
      }
      let commitPayload = commitPayloadRef.current;
      if (commitPayload === null) {
        const source = await client.readReimportSource(
          storeId,
          document.documentId,
          prepared.intentId,
        );
        if ((await sha256Hex(source)) !== prepared.sourceSha256) {
          throw new Error("The staged external Markdown changed before replacement.");
        }
        const initialized = await bootstrapCoworkYdoc(source);
        if (!initialized.ok) throw new Error(initialized.message);
        commitPayload = {
          snapshot: initialized.snapshot,
          snapshotSha256: initialized.snapshotSha256,
        };
        commitPayloadRef.current = commitPayload;
      }
      const receipt = await client.commitReimport(
        storeId,
        document.documentId,
        prepared,
        commitPayload.snapshot,
        commitPayload.snapshotSha256,
      );
      committedRef.current = receipt;
      await onReimported(receipt);
      committedRef.current = null;
      preparedRef.current = null;
      commitPayloadRef.current = null;
    } catch (commitError) {
      setError(asCoworkApiError(commitError).message);
      // Preserve the actor-bound prepared intent. Retry reuses the same staged source and
      // commit route; a response-lost commit is recovered by the same idempotent operation.
      setStage("confirm");
    }
  };

  const replacementBlockedReason = useMemo(() => {
    if (localBlockedReason !== null) return localBlockedReason;
    if (drift === null) return null;
    if (drift.unmaterializedStructuredEdits) {
      return "Save or sync the current Co-work edits before replacing this document.";
    }
    if (drift.state === "missing") return "Restore the Markdown file before re-importing it.";
    if (drift.state !== "drifted") return "The Markdown now matches Co-work; no replacement is needed.";
    return drift.canReimport ? null : "This document is not currently safe to replace.";
  }, [drift, localBlockedReason]);

  return (
    <ModalOverlay
      isOpen
      isDismissable={!busy}
      onOpenChange={(open) => {
        if (!open && !busy) close();
      }}
      className="wb-cowork-dialog-overlay"
    >
      <Modal className="wb-cowork-dialog wb-cowork-dialog--drift">
        <Dialog aria-labelledby="cowork-reimport-title" className="wb-cowork-dialog__body">
          <Heading id="cowork-reimport-title" slot="title">
            Review external Markdown changes
          </Heading>
          <p>
            <strong>{document.title}</strong> changed outside Co-work. Review the redline
            before deciding which version becomes the Co-work document.
          </p>
          {comparison.length > 0 ? (
            <pre className="wb-cowork-dialog__redline" aria-label="Markdown changes">
              {comparison}
            </pre>
          ) : null}
          {replacementBlockedReason !== null ? (
            <InlineAlert tone="warning">{replacementBlockedReason}</InlineAlert>
          ) : null}
          {preparedRef.current !== null && stage === "confirm" ? (
            <InlineAlert tone="warning">
              <strong>Replacement confirmation</strong>
              <span>{preparedRef.current.consequence}</span>
            </InlineAlert>
          ) : null}
          {error !== null ? <InlineAlert tone="danger" role="alert">{error}</InlineAlert> : null}
          {busy ? <p role="status"><Spinner /> {stage === "loading" ? "Comparing exact Markdown…" : stage === "preparing" ? "Preparing replacement…" : "Replacing Co-work document…"}</p> : null}
          <div className="wb-cowork-dialog__actions">
            <Button onClick={close} disabled={busy}>Cancel</Button>
            {stage === "confirm" ? (
              <Button
                variant="primary"
                onClick={() => void commit()}
                disabled={busy || replacementBlockedReason !== null}
              >
                {error === null ? "Replace Co-work document" : "Retry replacement"}
              </Button>
            ) : (
              <Button
                variant="primary"
                onClick={() => void prepare()}
                disabled={busy || replacementBlockedReason !== null || drift === null}
              >
                Continue to replacement
              </Button>
            )}
          </div>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
