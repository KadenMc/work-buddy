import { useEffect, useMemo, useRef, useState } from "react";
import * as Y from "yjs";
import {
  Dialog,
  Heading,
  Input,
  Label,
  ListBox,
  ListBoxItem,
  Modal,
  ModalOverlay,
  Text,
  TextField,
  type Key,
} from "react-aria-components";

import { Button, InlineAlert, Spinner } from "../../../ui";
import type { CoworkDocumentSummary, CoworkFolderSummary } from "../contracts";
import type { CoworkScratchPromotionContent } from "../editor/CoworkEditorPane";
import {
  CoworkHttpClient,
  type CoworkBootstrapPrepared,
  type CoworkCandidateDocument,
} from "../providers/CoworkHttpClient";
import { asCoworkApiError, coworkErrorMessage } from "../providers/errors";
import { sha256Hex } from "../persistence/hashing";
import { bootstrapCoworkYdoc } from "./bootstrapCoworkYdoc";

export type CoworkLifecycleDialogMode = "create" | "register" | "repair";

const slugPath = (title: string): string => {
  const slug = title
    .trim()
    .toLocaleLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `${slug || "untitled"}.md`;
};

const WINDOWS_RESERVED_SEGMENT =
  /^(?:con|prn|aux|nul|clock\$|com[1-9]|lpt[1-9])(?:\.|$)/iu;

export const validRelativeMarkdownPath = (value: string): boolean => {
  if (
    value.length === 0 ||
    /^(?:[a-z]:|[/\\])/iu.test(value) ||
    !/\.(?:md|markdown)$/iu.test(value)
  ) {
    return false;
  }
  const segments = value.split(/[\\/]/u);
  return segments.every(
    (segment) =>
      segment.length > 0 &&
      segment !== "." &&
      segment !== ".." &&
      !/[<>:"|?*\u0000-\u001f]/u.test(segment) &&
      !/[. ]$/u.test(segment) &&
      !WINDOWS_RESERVED_SEGMENT.test(segment),
  );
};

const makeIdempotencyKey = (): string =>
  globalThis.crypto?.randomUUID?.() ??
  `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;

interface CoworkDocumentLifecycleDialogProps {
  readonly mode: CoworkLifecycleDialogMode;
  readonly folder: CoworkFolderSummary;
  readonly client: CoworkHttpClient;
  readonly initialTitle?: string;
  readonly initialContent?: CoworkScratchPromotionContent;
  readonly repairDocument?: CoworkDocumentSummary;
  readonly onClose: () => void;
  readonly onOpened: (document: CoworkDocumentSummary) => Promise<void> | void;
}

export function CoworkDocumentLifecycleDialog({
  mode,
  folder,
  client,
  initialTitle = "",
  initialContent,
  repairDocument,
  onClose,
  onOpened,
}: CoworkDocumentLifecycleDialogProps) {
  const [title, setTitle] = useState(
    mode === "repair" ? repairDocument?.title ?? "" : initialTitle,
  );
  const [path, setPath] = useState(
    mode === "repair" ? repairDocument?.path ?? "" : "",
  );
  const [pathEdited, setPathEdited] = useState(mode !== "create");
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<readonly CoworkCandidateDocument[]>([]);
  const [candidateStatus, setCandidateStatus] = useState<"idle" | "loading" | "error">(
    mode === "register" ? "loading" : "idle",
  );
  const [stage, setStage] = useState<
    "idle" | "preparing" | "reading" | "building" | "committing" | "opening"
  >("idle");
  const [error, setError] = useState<string | null>(null);
  const loadEpoch = useRef(0);
  const operationRef = useRef<{
    readonly fingerprint: string;
    readonly key: string;
  } | null>(null);
  const preparedRef = useRef<CoworkBootstrapPrepared | null>(null);
  const busy = stage !== "idle";

  const closeDialog = (): void => {
    const prepared = preparedRef.current;
    preparedRef.current = null;
    if (prepared !== null && prepared.state !== "committed") {
      void client.cancelBootstrap(folder.storeId, prepared.bootstrapId).catch(() => undefined);
    }
    onClose();
  };

  useEffect(() => {
    if (mode !== "register") return;
    const epoch = ++loadEpoch.current;
    const timer = setTimeout(() => {
      setCandidateStatus("loading");
      void client
        .listCandidates(folder.storeId, query)
        .then((result) => {
          if (epoch !== loadEpoch.current) return;
          setCandidates(result.candidates);
          setCandidateStatus("idle");
        })
        .catch(() => {
          if (epoch !== loadEpoch.current) return;
          setCandidateStatus("error");
        });
    }, 150);
    return () => clearTimeout(timer);
  }, [client, folder.storeId, mode, query]);

  const derivedPath = useMemo(() => slugPath(title), [title]);
  const shownPath = pathEdited ? path : derivedPath;
  const stageLabel = {
    idle: "",
    preparing:
      mode === "create"
        ? "Preparing document…"
        : mode === "repair"
          ? "Preparing safe repair…"
          : "Reading Markdown…",
    reading: "Reading Markdown…",
    building: "Preparing document…",
    committing:
      mode === "create"
        ? "Creating document…"
        : mode === "repair"
          ? "Repairing document…"
          : "Adding document…",
    opening: "Opening document…",
  }[stage];

  const selectCandidate = (key: Key | null): void => {
    if (key === null) return;
    const selected = candidates.find((candidate) => candidate.path === String(key));
    if (selected === undefined) return;
    setPath(selected.path);
    setPathEdited(true);
    if (title.trim().length === 0) setTitle(selected.title.replace(/\.(?:md|markdown)$/i, ""));
  };

  const submit = async (): Promise<void> => {
    const normalizedTitle = title.trim();
    const normalizedPath = shownPath.replace(/\\/g, "/").trim();
    if (mode === "repair" && repairDocument === undefined) {
      setError("Choose the document to repair.");
      return;
    }
    if (mode === "create" && normalizedTitle.length === 0) {
      setError("Enter a title.");
      return;
    }
    if (!validRelativeMarkdownPath(normalizedPath)) {
      setError(
        "Use a safe relative .md or .markdown location without reserved names or characters.",
      );
      return;
    }

    setError(null);
    let prepared: CoworkBootstrapPrepared | undefined;
    try {
      setStage("preparing");
      const source =
        mode === "create"
          ? initialContent?.sourceBytes ?? new Uint8Array(0)
          : undefined;
      const initialSourceSha256 =
        source === undefined ? undefined : await sha256Hex(source);
      const operationFingerprint = JSON.stringify({
        mode,
        path: normalizedPath,
        title: normalizedTitle,
        initialSourceSha256,
      });
      if (operationRef.current?.fingerprint !== operationFingerprint) {
        const prior = preparedRef.current;
        preparedRef.current = null;
        if (prior !== null && prior.state !== "committed") {
          await client.cancelBootstrap(folder.storeId, prior.bootstrapId).catch(() => undefined);
        }
        operationRef.current = {
          fingerprint: operationFingerprint,
          key: makeIdempotencyKey(),
        };
      }
      prepared = await client.prepareBootstrap(
        folder.storeId,
        {
          mode: mode === "create" ? "create" : mode === "repair" ? "repair" : "import",
          path: normalizedPath,
          ...(normalizedTitle.length === 0 ? {} : { title: normalizedTitle }),
          ...(initialSourceSha256 === undefined
            ? {}
            : { initialSourceSha256 }),
          expectedFileSha256:
            mode === "repair" ? repairDocument?.currentFileSha256 ?? null : null,
          documentId: mode === "repair" ? repairDocument?.documentId ?? null : null,
          idempotencyKey: operationRef.current.key,
        },
        source,
      );
      preparedRef.current = prepared;
      if (prepared.state === "committed") {
        if (prepared.result === null) {
          throw new Error("The completed document receipt is unavailable.");
        }
        preparedRef.current = null;
        setStage("opening");
        await onOpened(prepared.result);
        onClose();
        return;
      }
      setStage("reading");
      const sourceBytes = await client.readBootstrapSource(prepared.sourceUrl);
      setStage("building");
      let snapshot: Uint8Array;
      let snapshotSha256: string;
      const stagedSourceSha256 = await sha256Hex(sourceBytes);
      if (stagedSourceSha256 !== prepared.sourceSha256) {
        throw new Error("The Markdown source changed while Co-work was preparing it.");
      }
      if (initialContent === undefined) {
        const initialized = await bootstrapCoworkYdoc(sourceBytes);
        if (!initialized.ok) throw new Error(initialized.message);
        snapshot = initialized.snapshot;
        snapshotSha256 = initialized.snapshotSha256;
      } else {
        if ((await sha256Hex(initialContent.sourceBytes)) !== stagedSourceSha256) {
          throw new Error("The document changed while Co-work was preparing it.");
        }
        const validationDoc = new Y.Doc();
        try {
          Y.applyUpdate(validationDoc, initialContent.snapshot);
          if (
            validationDoc
              .getMap<unknown>("wb-cowork:fidelity")
              .get("source_sha256") !== stagedSourceSha256
          ) {
            throw new Error("The saved document data doesn’t match its Markdown.");
          }
        } catch (snapshotError) {
          if (snapshotError instanceof Error) throw snapshotError;
          throw new Error("The document couldn’t be prepared.");
        } finally {
          validationDoc.destroy();
        }
        snapshot = initialContent.snapshot;
        snapshotSha256 = await sha256Hex(snapshot);
      }
      setStage("committing");
      const document = await client.commitBootstrap(
        folder.storeId,
        prepared,
        snapshot,
        snapshotSha256,
      );
      preparedRef.current = null;
      setStage("opening");
      await onOpened(document);
      onClose();
    } catch (submitError) {
      // Retain a prepared/ambiguously committed intent and its stable key. Retry can then
      // recover the same staged source or the actor-scoped committed receipt.
      setError(
        coworkErrorMessage(
          asCoworkApiError(submitError),
          mode === "register"
            ? "Co-work couldn’t add that Markdown file."
            : mode === "repair"
              ? "Co-work couldn’t repair that document."
              : "Co-work couldn’t create that document.",
        ),
      );
      setStage("idle");
    }
  };

  return (
    <ModalOverlay
      isOpen
      isDismissable={!busy}
      onOpenChange={(open) => {
        if (!open && !busy) closeDialog();
      }}
      className="wb-cowork-dialog-overlay"
    >
      <Modal className="wb-cowork-dialog">
        <Dialog aria-labelledby="cowork-lifecycle-dialog-title" className="wb-cowork-dialog__body">
          <Heading id="cowork-lifecycle-dialog-title" slot="title">
            {mode === "create"
              ? "New document"
              : mode === "repair"
                ? "Repair document"
                : "Add Markdown document"}
          </Heading>
          <p className="wb-cowork-dialog__folder">
            <strong title={folder.folderPath}>{folder.folderName}</strong>
          </p>
          {mode === "register" ? (
            <p>
              Choose a Markdown file from this folder. Co-work will keep editing the same
              file.
            </p>
          ) : mode === "repair" ? (
            <InlineAlert tone="warning">
              Co-work will rebuild this document’s editing data from the current Markdown.
              The Markdown file itself will not be rewritten or deleted.
            </InlineAlert>
          ) : null}

          {error !== null ? (
            <InlineAlert tone="danger" role="alert">
              {error}
            </InlineAlert>
          ) : null}

          {mode === "register" ? (
            <>
              <TextField value={query} onChange={setQuery} className="wb-cowork-field">
                <Label>Find Markdown</Label>
                <Input autoFocus placeholder="Search this folder" />
              </TextField>
              {candidateStatus === "loading" ? (
                <p role="status"><Spinner /> Looking for Markdown files…</p>
              ) : candidateStatus === "error" ? (
                <InlineAlert tone="warning">
                  Co-work couldn’t list Markdown files. Enter the file’s location below.
                </InlineAlert>
              ) : candidates.length === 0 ? (
                <p className="wb-cowork-dialog__empty">No matching Markdown files.</p>
              ) : (
                <ListBox
                  aria-label="Markdown files"
                  selectionMode="single"
                  selectedKeys={path.length === 0 ? [] : [path]}
                  onSelectionChange={(keys) => {
                    if (keys === "all") return;
                    selectCandidate([...keys][0] ?? null);
                  }}
                  className="wb-cowork-candidates"
                >
                  {candidates.map((candidate) => (
                    <ListBoxItem
                      key={candidate.path}
                      id={candidate.path}
                      textValue={candidate.path}
                      className="wb-cowork-candidates__item"
                    >
                      <span>{candidate.path}</span>
                      <small>{candidate.byteSize.toLocaleString()} bytes</small>
                    </ListBoxItem>
                  ))}
                </ListBox>
              )}
            </>
          ) : null}

          {mode !== "repair" ? <TextField
            value={title}
            onChange={(next) => {
              setTitle(next);
              if (!pathEdited && mode === "create") setPath(slugPath(next));
            }}
            isRequired={mode === "create"}
            className="wb-cowork-field"
          >
            <Label>{mode === "create" ? "Title" : "Display title (optional)"}</Label>
            <Input autoFocus={mode === "create"} />
          </TextField> : null}
          {mode !== "repair" ? <TextField
            value={shownPath}
            onChange={(next) => {
              setPath(next);
              setPathEdited(true);
            }}
            isRequired
            className="wb-cowork-field"
          >
            <Label>Location inside {folder.folderName}</Label>
            <Input placeholder="notes/example.md" />
            <Text slot="description">{folder.folderName} / {shownPath || "…"}</Text>
          </TextField> : (
            <p className="wb-cowork-dialog__folder">
              <strong>{repairDocument?.title ?? "Document"}</strong>
              <span>{repairDocument?.path ?? ""}</span>
            </p>
          )}

          {busy ? <p role="status" className="wb-cowork-dialog__progress"><Spinner /> {stageLabel}</p> : null}
          <div className="wb-cowork-dialog__actions">
            <Button onClick={closeDialog} disabled={busy}>Cancel</Button>
            <Button variant="primary" onClick={() => void submit()} disabled={busy}>
              {mode === "create"
                ? "Create document"
                : mode === "repair"
                  ? "Repair document"
                  : "Add document"}
            </Button>
          </div>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
