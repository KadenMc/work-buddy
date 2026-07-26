import { FolderSimple } from "@phosphor-icons/react/FolderSimple";
import { useEffect, useMemo, useRef, useState } from "react";
import * as Y from "yjs";
import {
  Dialog,
  Heading,
  Input,
  Label,
  Modal,
  ModalOverlay,
  Text,
  TextField,
} from "react-aria-components";

import { Button, InlineAlert, Spinner } from "../../../ui";
import type {
  CoworkApiError,
  CoworkDocumentSummary,
  CoworkFolderSummary,
} from "../contracts";
import type { CoworkScratchPromotionContent } from "../editor/CoworkEditorPane";
import {
  CoworkHttpClient,
  type CoworkBootstrapPrepared,
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

const normalizedRelativePath = (value: string): string =>
  value.replace(/\\/gu, "/").replace(/^\.\/+/u, "").replace(/\/+/gu, "/").trim();

const fileNameFromPath = (value: string): string => {
  const parts = normalizedRelativePath(value).split("/");
  return parts[parts.length - 1] ?? "";
};

const titleFromMarkdownPath = (value: string): string =>
  fileNameFromPath(value).replace(/\.(?:md|markdown)$/iu, "") || "Untitled";

const joinedRelativePath = (directory: string, fileName: string): string => {
  const normalizedDirectory = normalizedRelativePath(directory).replace(/\/+$/u, "");
  const normalizedFileName = fileName.trim();
  return normalizedDirectory.length === 0
    ? normalizedFileName
    : `${normalizedDirectory}/${normalizedFileName}`;
};

const comparablePath = (value: string): string =>
  normalizedRelativePath(value).normalize("NFC");

const isWindowsFolderPath = (value: string): boolean =>
  /^(?:[a-z]:[\\/]|\\\\)/iu.test(value);

/**
 * Match the server's path identity rule without making POSIX Folder paths
 * case-insensitive. The server remains authoritative for unusual Unicode
 * case-folding and races during bootstrap.
 */
export const sameMarkdownPath = (
  left: string,
  right: string,
  folderPath: string,
): boolean => {
  const comparableLeft = comparablePath(left);
  const comparableRight = comparablePath(right);
  return isWindowsFolderPath(folderPath)
    ? comparableLeft.toLocaleLowerCase("en-US") ===
        comparableRight.toLocaleLowerCase("en-US")
    : comparableLeft === comparableRight;
};

const pickerErrorMessage = (
  error: CoworkApiError,
  kind: "markdown" | "location",
): string => {
  const noun = kind === "markdown" ? "Markdown picker" : "location picker";
  const messages: Readonly<Record<string, string>> = {
    folder_chooser_busy: `Another picker is already open. Close it before opening the ${noun}.`,
    folder_chooser_timeout: `The ${noun} took too long. Try again.`,
    folder_chooser_unavailable:
      kind === "markdown"
        ? "Markdown file selection isn’t available here."
        : "Folder selection isn’t available here.",
    folder_chooser_failed: `The ${noun} couldn’t be opened.`,
    markdown_outside_folder: "Choose a Markdown file inside the active Folder.",
    markdown_file_unavailable: "That Markdown file is no longer available.",
    invalid_markdown_file: "Choose a .md or .markdown file.",
    location_outside_folder: "Choose a location inside the active Folder.",
    location_unavailable: "That location is no longer available.",
    managed_location: "Choose a document folder outside Co-work’s support data.",
  };
  return messages[error.code] ?? coworkErrorMessage(error, `The ${noun} couldn’t be opened.`);
};

const pickerCanRetry = (error: CoworkApiError): boolean =>
  error.retryable ||
  [
    "folder_chooser_busy",
    "folder_chooser_timeout",
    "folder_chooser_failed",
    "markdown_outside_folder",
    "markdown_file_unavailable",
    "invalid_markdown_file",
    "location_outside_folder",
    "location_unavailable",
    "managed_location",
  ].includes(error.code);

interface CoworkDocumentLifecycleDialogProps {
  readonly mode: CoworkLifecycleDialogMode;
  readonly folder: CoworkFolderSummary;
  readonly client: CoworkHttpClient;
  readonly markdownPickerAvailable?: boolean;
  readonly locationPickerAvailable?: boolean;
  readonly initialTitle?: string;
  readonly initialContent?: CoworkScratchPromotionContent;
  readonly repairDocument?: CoworkDocumentSummary;
  readonly onClose: () => void;
  readonly onOpened: (document: CoworkDocumentSummary) => Promise<void> | void;
}

type BootstrapStage =
  | "idle"
  | "checking"
  | "preparing"
  | "reading"
  | "building"
  | "committing"
  | "opening";

interface PickerFailure {
  readonly message: string;
  readonly retryable: boolean;
}

export function CoworkDocumentLifecycleDialog({
  mode,
  folder,
  client,
  markdownPickerAvailable = true,
  locationPickerAvailable = true,
  initialTitle = "",
  initialContent,
  repairDocument,
  onClose,
  onOpened,
}: CoworkDocumentLifecycleDialogProps) {
  const [title, setTitle] = useState(
    mode === "repair" ? repairDocument?.title ?? "" : initialTitle,
  );
  const [fileName, setFileName] = useState(
    mode === "repair"
      ? fileNameFromPath(repairDocument?.path ?? "")
      : slugPath(initialTitle),
  );
  const [fileNameEdited, setFileNameEdited] = useState(mode !== "create");
  const [destinationDirectory, setDestinationDirectory] = useState("");
  const [selectedMarkdownPath, setSelectedMarkdownPath] = useState<string | null>(
    mode === "repair" ? repairDocument?.path ?? null : null,
  );
  const [pickerOpening, setPickerOpening] = useState(false);
  const [pickerFailure, setPickerFailure] = useState<PickerFailure | null>(null);
  const [locationOpening, setLocationOpening] = useState(false);
  const [locationFailure, setLocationFailure] = useState<PickerFailure | null>(null);
  const [stage, setStage] = useState<BootstrapStage>("idle");
  const [error, setError] = useState<string | null>(null);
  const initialPickerStarted = useRef(false);
  const pickerEpoch = useRef(0);
  const locationEpoch = useRef(0);
  const operationRef = useRef<{
    readonly fingerprint: string;
    readonly key: string;
  } | null>(null);
  const preparedRef = useRef<CoworkBootstrapPrepared | null>(null);
  const busy = stage !== "idle" || pickerOpening || locationOpening;

  const shownFileName = fileNameEdited ? fileName : slugPath(title);
  const shownPath = useMemo(
    () =>
      mode === "repair"
        ? repairDocument?.path ?? ""
        : mode === "register"
          ? selectedMarkdownPath ?? ""
          : joinedRelativePath(destinationDirectory, shownFileName),
    [
      destinationDirectory,
      mode,
      repairDocument?.path,
      selectedMarkdownPath,
      shownFileName,
    ],
  );
  const destinationLabel =
    destinationDirectory.length === 0
      ? folder.folderName
      : `${folder.folderName} / ${destinationDirectory}`;

  const closeDialog = (): void => {
    pickerEpoch.current += 1;
    locationEpoch.current += 1;
    const prepared = preparedRef.current;
    preparedRef.current = null;
    if (prepared !== null && prepared.state !== "committed") {
      void client.cancelBootstrap(folder.storeId, prepared.bootstrapId).catch(() => undefined);
    }
    onClose();
  };

  const stageLabel: Readonly<Record<BootstrapStage, string>> = {
    idle: "",
    checking: "Checking document…",
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
          : "Creating document…",
    opening: "Opening document…",
  };

  const bootstrapDocument = async (
    normalizedTitle: string,
    normalizedPath: string,
  ): Promise<void> => {
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
      const apiError = asCoworkApiError(submitError);
      const existingDocumentId = apiError.details?.document_id;
      if (
        mode === "register" &&
        apiError.code === "already_registered" &&
        typeof existingDocumentId === "string"
      ) {
        try {
          const existing = await client.readDocument(
            folder.storeId,
            existingDocumentId,
          );
          setStage("opening");
          await onOpened(existing);
          onClose();
          return;
        } catch {
          // Keep the authoritative registration conflict visible if its document
          // could not be recovered. A retry can re-check the catalog.
        }
      }
      // Retain a prepared/ambiguously committed intent and its stable key. Retry can then
      // recover the same staged source or the actor-scoped committed receipt.
      setError(
        coworkErrorMessage(
          apiError,
          mode === "register"
            ? "Co-work couldn’t create a document from that Markdown file."
            : mode === "repair"
              ? "Co-work couldn’t repair that document."
              : "Co-work couldn’t create that document.",
        ),
      );
      setStage("idle");
    }
  };

  const registerMarkdown = async (path: string): Promise<void> => {
    const normalizedPath = normalizedRelativePath(path);
    setSelectedMarkdownPath(normalizedPath);
    setPickerFailure(null);
    setError(null);
    if (!validRelativeMarkdownPath(normalizedPath)) {
      setError("Choose a .md or .markdown file inside the active Folder.");
      return;
    }
    try {
      setStage("checking");
      const documents = await client.listDocuments(folder.storeId);
      const existing = documents.find(
        (document) =>
          sameMarkdownPath(document.path, normalizedPath, folder.folderPath),
      );
      if (existing !== undefined) {
        setStage("opening");
        await onOpened(existing);
        onClose();
        return;
      }
      await bootstrapDocument(titleFromMarkdownPath(normalizedPath), normalizedPath);
    } catch (registerError) {
      setError(
        coworkErrorMessage(
          asCoworkApiError(registerError),
          "Co-work couldn’t check whether that Markdown file is already open in Co-work.",
        ),
      );
      setStage("idle");
    }
  };

  const chooseMarkdown = async (): Promise<void> => {
    if (!markdownPickerAvailable || pickerOpening || stage !== "idle") return;
    const epoch = ++pickerEpoch.current;
    setPickerOpening(true);
    setPickerFailure(null);
    setError(null);
    try {
      const result = await client.chooseMarkdownFile(folder.storeId);
      if (epoch !== pickerEpoch.current) return;
      setPickerOpening(false);
      if (result.cancelled) {
        closeDialog();
        return;
      }
      await registerMarkdown(result.path);
    } catch (pickerError) {
      if (epoch !== pickerEpoch.current) return;
      const apiError = asCoworkApiError(pickerError);
      setPickerOpening(false);
      setPickerFailure({
        message: pickerErrorMessage(apiError, "markdown"),
        retryable: pickerCanRetry(apiError),
      });
    }
  };

  const chooseLocation = async (): Promise<void> => {
    if (!locationPickerAvailable || locationOpening || stage !== "idle") return;
    const epoch = ++locationEpoch.current;
    setLocationOpening(true);
    setLocationFailure(null);
    setError(null);
    try {
      const result = await client.chooseLocation(folder.storeId);
      if (epoch !== locationEpoch.current) return;
      setLocationOpening(false);
      if (result.cancelled) return;
      // Empty is intentional: it represents the active Folder root.
      setDestinationDirectory(normalizedRelativePath(result.path).replace(/\/+$/u, ""));
    } catch (pickerError) {
      if (epoch !== locationEpoch.current) return;
      const apiError = asCoworkApiError(pickerError);
      setLocationOpening(false);
      setLocationFailure({
        message: pickerErrorMessage(apiError, "location"),
        retryable: pickerCanRetry(apiError),
      });
    }
  };

  const submit = async (): Promise<void> => {
    const normalizedTitle = title.trim();
    const normalizedPath = normalizedRelativePath(shownPath);
    if (mode === "repair" && repairDocument === undefined) {
      setError("Choose the document to repair.");
      return;
    }
    if (mode === "create" && normalizedTitle.length === 0) {
      setError("Enter a title.");
      return;
    }
    if (
      mode === "create" &&
      (shownFileName.trim().length === 0 || /[/\\]/u.test(shownFileName))
    ) {
      setError("Enter a filename without folder separators.");
      return;
    }
    if (!validRelativeMarkdownPath(normalizedPath)) {
      setError(
        mode === "create"
          ? "Use a safe .md or .markdown filename without reserved names or characters."
          : "Use a safe relative .md or .markdown location without reserved names or characters.",
      );
      return;
    }
    await bootstrapDocument(normalizedTitle, normalizedPath);
  };

  useEffect(() => {
    if (mode !== "register" || initialPickerStarted.current) return;
    initialPickerStarted.current = true;
    if (markdownPickerAvailable) void chooseMarkdown();
  });

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
                : "New document from Markdown"}
          </Heading>
          <p className="wb-cowork-dialog__folder">
            <strong title={folder.folderPath}>{folder.folderName}</strong>
          </p>
          {mode === "register" ? (
            <p>
              Choose a Markdown file in this Folder. Co-work uses the original file;
              no copy is made.
            </p>
          ) : mode === "repair" ? (
            <InlineAlert tone="warning">
              Co-work will rebuild this document’s editing data from the current Markdown.
              The Markdown file itself will not be rewritten or deleted.
            </InlineAlert>
          ) : null}
          {mode === "register" && !markdownPickerAvailable ? (
            <InlineAlert id="cowork-markdown-picker-unavailable" tone="warning">
              Markdown file selection isn’t available here.
            </InlineAlert>
          ) : null}

          {pickerFailure !== null ? (
            <InlineAlert tone="danger" role="alert">
              {pickerFailure.message}
            </InlineAlert>
          ) : error !== null ? (
            <InlineAlert tone="danger" role="alert">
              {error}
            </InlineAlert>
          ) : null}

          {mode === "register" ? (
            <>
              {selectedMarkdownPath !== null ? (
                <p className="wb-cowork-dialog__selection" title={selectedMarkdownPath}>
                  <strong>{fileNameFromPath(selectedMarkdownPath)}</strong>
                  <span>{selectedMarkdownPath}</span>
                </p>
              ) : null}
              {pickerOpening ? (
                <p role="status" className="wb-cowork-dialog__progress">
                  <Spinner /> Opening Markdown picker…
                </p>
              ) : null}
            </>
          ) : mode !== "repair" ? (
            <>
              <TextField
                value={title}
                onChange={(next) => {
                  setTitle(next);
                  if (!fileNameEdited) setFileName(slugPath(next));
                }}
                isRequired
                className="wb-cowork-field"
              >
                <Label>Title</Label>
                <Input autoFocus />
              </TextField>

              <div
                className="wb-cowork-field"
                role="group"
                aria-labelledby="cowork-save-in-label"
              >
                <span id="cowork-save-in-label">Save in</span>
                <div className="wb-cowork-dialog__destination">
                  <span title={destinationLabel}>
                    <FolderSimple aria-hidden="true" />
                    <strong>{destinationLabel}</strong>
                  </span>
                  <Button
                    size="small"
                    onClick={() => void chooseLocation()}
                    disabled={busy || !locationPickerAvailable}
                    aria-describedby={
                      locationPickerAvailable
                        ? undefined
                        : "cowork-location-picker-unavailable"
                    }
                    title={
                      locationPickerAvailable
                        ? "Choose another location in this Folder."
                        : "Choosing another save location isn’t available here."
                    }
                  >
                    {locationOpening ? "Opening…" : "Change"}
                  </Button>
                </div>
              </div>
              {!locationPickerAvailable ? (
                <InlineAlert
                  id="cowork-location-picker-unavailable"
                  tone="warning"
                >
                  Choosing another save location isn’t available here. You can
                  still save in {folder.folderName}.
                </InlineAlert>
              ) : null}
              {locationFailure !== null ? (
                <InlineAlert tone="danger" role="alert">
                  <span>{locationFailure.message}</span>
                  {locationFailure.retryable ? (
                    <Button size="small" onClick={() => void chooseLocation()}>
                      Try again
                    </Button>
                  ) : null}
                </InlineAlert>
              ) : null}

              <TextField
                value={shownFileName}
                onChange={(next) => {
                  setFileName(next);
                  setFileNameEdited(true);
                }}
                isRequired
                className="wb-cowork-field"
              >
                <Label>File name</Label>
                <Input />
                <Text slot="description">
                  {folder.folderName} / {shownPath || "untitled.md"}
                </Text>
              </TextField>
            </>
          ) : (
            <p className="wb-cowork-dialog__folder">
              <strong>{repairDocument?.title ?? "Document"}</strong>
              <span>{repairDocument?.path ?? ""}</span>
            </p>
          )}

          {stage !== "idle" ? (
            <p role="status" className="wb-cowork-dialog__progress">
              <Spinner /> {stageLabel[stage]}
            </p>
          ) : null}
          <div className="wb-cowork-dialog__actions">
            <Button onClick={closeDialog} disabled={busy}>Cancel</Button>
            {mode === "register" ? (
              pickerFailure !== null && pickerFailure.retryable ? (
                <Button variant="primary" onClick={() => void chooseMarkdown()}>
                  Choose again
                </Button>
              ) : error !== null && selectedMarkdownPath !== null ? (
                <>
                  <Button onClick={() => void chooseMarkdown()}>Choose another file</Button>
                  <Button
                    variant="primary"
                    onClick={() => void registerMarkdown(selectedMarkdownPath)}
                  >
                    Try again
                  </Button>
                </>
              ) : null
            ) : (
              <Button variant="primary" onClick={() => void submit()} disabled={busy}>
                {mode === "create" ? "Create document" : "Repair document"}
              </Button>
            )}
          </div>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
