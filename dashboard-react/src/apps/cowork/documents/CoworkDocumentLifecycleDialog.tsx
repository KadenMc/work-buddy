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
  type CoworkImportDescriptor,
} from "../providers/CoworkHttpClient";
import {
  asCoworkApiError,
  CoworkHttpError,
  coworkErrorMessage,
} from "../providers/errors";
import { sha256Hex } from "../persistence/hashing";
import {
  CoworkProvenanceForm,
  coworkProvenanceDeterminationIssue,
  unknownCoworkProvenanceDetermination,
  type CoworkProvenanceActorIdentity,
  type CoworkProvenanceDetermination,
} from "../provenance";
import { bootstrapCoworkYdoc } from "./bootstrapCoworkYdoc";
import {
  coworkFileConverter,
  coworkImportedTitleFromPath,
} from "./fileImporters";

export type CoworkLifecycleDialogMode = "create" | "import" | "repair";

const slugPath = (title: string): string => {
  const slug = title
    .trim()
    .toLocaleLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `${slug || "untitled"}.md`;
};

const makeIdempotencyKey = (): string =>
  globalThis.crypto?.randomUUID?.() ??
  `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;

const normalizedRelativePath = (value: string): string =>
  value.replace(/\\/gu, "/").replace(/^\.\/+/u, "").replace(/\/+/gu, "/").trim();

const WINDOWS_RESERVED_SEGMENT =
  /^(?:con|prn|aux|nul|clock\$|com[1-9]|lpt[1-9])(?:\.|$)/iu;

const validCreatedMarkdownPath = (value: string): boolean => {
  if (
    value.length === 0 ||
    /^(?:[a-z]:|[/\\])/iu.test(value) ||
    !/\.(?:md|markdown)$/iu.test(value)
  ) {
    return false;
  }
  return value.split(/[\\/]/u).every(
    (segment) =>
      segment.length > 0 &&
      segment !== "." &&
      segment !== ".." &&
      !/[<>:"|?*\u0000-\u001f]/u.test(segment) &&
      !/[. ]$/u.test(segment) &&
      !WINDOWS_RESERVED_SEGMENT.test(segment),
  );
};

const fileNameFromPath = (value: string): string => {
  const parts = normalizedRelativePath(value).split("/");
  return parts[parts.length - 1] ?? "";
};

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
export const sameFilePath = (
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
  kind: "file" | "location",
): string => {
  const noun = kind === "file" ? "file picker" : "location picker";
  const messages: Readonly<Record<string, string>> = {
    folder_chooser_busy: `Another picker is already open. Close it before opening the ${noun}.`,
    folder_chooser_timeout: `The ${noun} took too long. Try again.`,
    folder_chooser_unavailable:
      kind === "file"
        ? "File import isn’t available here."
        : "Choosing a folder isn’t available here.",
    folder_chooser_failed: `The ${noun} couldn’t be opened.`,
    markdown_outside_folder: "Choose a supported file inside the active folder.",
    markdown_file_unavailable: "That file is no longer available.",
    invalid_markdown_file: "Choose a .md or .markdown file.",
    file_outside_folder: "Choose a supported file inside the active folder.",
    file_unavailable: "That file is no longer available.",
    invalid_import_file: "Choose a supported file inside the active folder.",
    unsupported_file_type: "That file type isn’t supported yet.",
    importer_version_unavailable:
      "This Co-work app doesn’t include the converter version selected for that file. Refresh or update Co-work, then try again.",
    import_source_too_large: "That file is too large to import.",
    location_outside_folder: "Choose a location inside the active folder.",
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
    "file_outside_folder",
    "file_unavailable",
    "invalid_import_file",
    "unsupported_file_type",
    "importer_version_unavailable",
    "import_source_too_large",
    "location_outside_folder",
    "location_unavailable",
    "managed_location",
  ].includes(error.code);

interface CoworkDocumentLifecycleDialogProps {
  readonly mode: CoworkLifecycleDialogMode;
  readonly folder: CoworkFolderSummary;
  readonly client: CoworkHttpClient;
  /** Injectable capture identity for tests and authenticated host shells. */
  readonly provenanceActor?: CoworkProvenanceActorIdentity;
  readonly filePickerAvailable?: boolean;
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

interface ExistingDetachedImportWarning {
  readonly document: CoworkDocumentSummary;
  readonly reason: "source_changed" | "identity_unavailable";
}

interface RetiredImportConflict {
  readonly documentId: string;
}

const detachedImportWarning = (
  document: CoworkDocumentSummary,
  selectedSourceSha256: string,
): ExistingDetachedImportWarning | null => {
  if (document.sourceWriteback !== "never") return null;
  if (
    document.importSourceSha256 === null ||
    document.importSourceSha256 === undefined
  ) {
    return { document, reason: "identity_unavailable" };
  }
  return document.importSourceSha256 === selectedSourceSha256
    ? null
    : { document, reason: "source_changed" };
};

export function CoworkDocumentLifecycleDialog({
  mode,
  folder,
  client,
  provenanceActor,
  filePickerAvailable = true,
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
  const [selectedFilePath, setSelectedFilePath] = useState<string | null>(
    mode === "repair" ? repairDocument?.path ?? null : null,
  );
  const [selectedImporter, setSelectedImporter] =
    useState<CoworkImportDescriptor | null>(null);
  const [selectedSourceSha256, setSelectedSourceSha256] = useState<string | null>(
    null,
  );
  const [existingImportWarning, setExistingImportWarning] =
    useState<ExistingDetachedImportWarning | null>(null);
  const [retiredImportConflict, setRetiredImportConflict] =
    useState<RetiredImportConflict | null>(null);
  const [authorshipAttestation, setAuthorshipAttestation] =
    useState<CoworkProvenanceDetermination>(
      unknownCoworkProvenanceDetermination,
    );
  const [currentActorIdentity, setCurrentActorIdentity] =
    useState<CoworkProvenanceActorIdentity | null>(null);
  const [identityFailure, setIdentityFailure] = useState<string | null>(null);
  const [pickerOpening, setPickerOpening] = useState(false);
  const [pickerFailure, setPickerFailure] = useState<PickerFailure | null>(null);
  const [locationOpening, setLocationOpening] = useState(false);
  const [locationFailure, setLocationFailure] = useState<PickerFailure | null>(null);
  const [stage, setStage] = useState<BootstrapStage>("idle");
  const [error, setError] = useState<string | null>(null);
  const initialFilePickerStarted = useRef(false);
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
        : mode === "import"
          ? selectedFilePath ?? ""
          : joinedRelativePath(destinationDirectory, shownFileName),
    [
      destinationDirectory,
      mode,
      repairDocument?.path,
      selectedFilePath,
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
          : "Reading file…",
    reading: "Reading file…",
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
    attestation?: CoworkProvenanceDetermination,
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
        importerId: selectedImporter?.importerId,
        sourceMediaType: selectedImporter?.mediaType,
        selectedSourceSha256,
        authorshipAttestation: attestation,
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
          ...(mode === "import" && selectedImporter !== null
            ? {
                importerId: selectedImporter.importerId,
                sourceMediaType: selectedImporter.mediaType,
                authorshipAttestation: attestation,
              }
            : {}),
          expectedFileSha256:
            mode === "repair"
              ? repairDocument?.currentFileSha256 ?? null
              : mode === "import"
                ? selectedSourceSha256
                : null,
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
      let projection: Uint8Array;
      let projectionSha256: string;
      const stagedSourceSha256 = await sha256Hex(sourceBytes);
      if (stagedSourceSha256 !== prepared.sourceSha256) {
        throw new Error("The source file changed while Co-work was preparing it.");
      }
      if (initialContent === undefined) {
        const selectedFileConverter =
          mode === "import" && selectedImporter !== null
            ? coworkFileConverter(selectedImporter.importerId)
            : null;
        if (mode === "import" && selectedFileConverter === null) {
          throw new CoworkHttpError({
            code: "importer_version_unavailable",
            message:
              "This Co-work app does not include the converter version selected by the server.",
            retryable: false,
          });
        }
        const initialized =
          selectedFileConverter === null
            ? await bootstrapCoworkYdoc(sourceBytes)
            : await selectedFileConverter.convert(sourceBytes);
        if (!initialized.ok) throw new Error(initialized.message);
        snapshot = initialized.snapshot;
        snapshotSha256 = initialized.snapshotSha256;
        projection = initialized.projection;
        projectionSha256 = initialized.projectionSha256;
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
        projection = initialContent.sourceBytes;
        projectionSha256 = stagedSourceSha256;
      }
      setStage("committing");
      const document = await client.commitBootstrap(
        folder.storeId,
        prepared,
        snapshot,
        snapshotSha256,
        projection,
        projectionSha256,
      );
      preparedRef.current = null;
      setStage("opening");
      await onOpened(document);
      onClose();
    } catch (submitError) {
      const apiError = asCoworkApiError(submitError);
      if (
        mode === "import" &&
        apiError.code === "provenance_actor_changed"
      ) {
        const stalePrepared = preparedRef.current;
        preparedRef.current = null;
        operationRef.current = null;
        if (stalePrepared !== null && stalePrepared.state !== "committed") {
          await client
            .cancelBootstrap(folder.storeId, stalePrepared.bootstrapId)
            .catch(() => undefined);
        }
        setCurrentActorIdentity(null);
        setAuthorshipAttestation(unknownCoworkProvenanceDetermination());
        setIdentityFailure(
          "The current identity changed. Check it again before importing.",
        );
        setError(null);
        setStage("idle");
        return;
      }
      const existingDocumentId = apiError.details?.document_id;
      if (
        mode === "import" &&
        apiError.code === "retired_path" &&
        typeof existingDocumentId === "string"
      ) {
        setRetiredImportConflict({ documentId: existingDocumentId });
        setExistingImportWarning(null);
        setError(null);
        setStage("idle");
        return;
      }
      if (
        mode === "import" &&
        apiError.code === "already_registered" &&
        typeof existingDocumentId === "string"
      ) {
        try {
          const existing = await client.readDocument(
            folder.storeId,
            existingDocumentId,
          );
          // Compatibility and race defense: an older server can report the
          // generic identity conflict, or the document can retire between that
          // response and this authoritative read. Never offer an open action
          // once the recovered lifecycle is terminal.
          if (existing.lifecycle === "retired") {
            setRetiredImportConflict({ documentId: existing.documentId });
            setExistingImportWarning(null);
            setError(null);
            setStage("idle");
            return;
          }
          if (selectedSourceSha256 !== null) {
            const warning = detachedImportWarning(
              existing,
              selectedSourceSha256,
            );
            if (warning !== null) {
              setExistingImportWarning(warning);
              setError(null);
              setStage("idle");
              return;
            }
          }
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
          mode === "import"
            ? "Co-work couldn’t create a document from that file."
            : mode === "repair"
              ? "Co-work couldn’t repair that document."
              : "Co-work couldn’t create that document.",
        ),
      );
      setStage("idle");
    }
  };

  const resolveImportActor = async (): Promise<void> => {
    setStage("checking");
    setIdentityFailure(null);
    setError(null);
    setCurrentActorIdentity(null);
    try {
      setCurrentActorIdentity(
        provenanceActor ?? (await client.currentActor()),
      );
    } catch (actorError) {
      setIdentityFailure(
        coworkErrorMessage(
          asCoworkApiError(actorError),
          "Co-work couldn’t check the current identity. Try again.",
        ),
      );
    } finally {
      setStage("idle");
    }
  };

  const selectImportFile = async (
    path: string,
    importer: CoworkImportDescriptor,
    sourceSha256: string,
  ): Promise<void> => {
    const normalizedPath = normalizedRelativePath(path);
    setPickerFailure(null);
    setError(null);
    if (coworkFileConverter(importer.importerId) === null) {
      throw new CoworkHttpError({
        code: "importer_version_unavailable",
        message:
          "This Co-work app does not include the converter version selected by the server.",
        retryable: false,
      });
    }
    setSelectedFilePath(normalizedPath);
    setSelectedImporter(importer);
    setSelectedSourceSha256(sourceSha256);
    setExistingImportWarning(null);
    setRetiredImportConflict(null);
    setAuthorshipAttestation(unknownCoworkProvenanceDetermination());
    setCurrentActorIdentity(null);
    setIdentityFailure(null);
    try {
      setStage("checking");
      const documents = await client.listDocuments(folder.storeId);
      const existing = documents.find(
        (document) =>
          sameFilePath(document.path, normalizedPath, folder.folderPath),
      );
      if (existing !== undefined) {
        if (existing.lifecycle === "retired") {
          setRetiredImportConflict({ documentId: existing.documentId });
          setStage("idle");
          return;
        }
        const warning = detachedImportWarning(existing, sourceSha256);
        if (warning !== null) {
          setExistingImportWarning(warning);
          setStage("idle");
          return;
        }
        setStage("opening");
        await onOpened(existing);
        onClose();
        return;
      }
      await resolveImportActor();
    } catch (catalogError) {
      setError(
        coworkErrorMessage(
          asCoworkApiError(catalogError),
          "Co-work couldn’t check whether that file is already open in Co-work.",
        ),
      );
      setStage("idle");
    }
  };

  const chooseFile = async (): Promise<void> => {
    if (!filePickerAvailable || pickerOpening || stage !== "idle") return;
    const hadSelection = selectedFilePath !== null;
    const epoch = ++pickerEpoch.current;
    setPickerOpening(true);
    setPickerFailure(null);
    setError(null);
    try {
      const result = await client.chooseImportFile(folder.storeId);
      if (epoch !== pickerEpoch.current) return;
      setPickerOpening(false);
      if (result.cancelled) {
        if (!hadSelection) closeDialog();
        return;
      }
      await selectImportFile(
        result.path,
        result.importer,
        result.sourceSha256,
      );
    } catch (pickerError) {
      if (epoch !== pickerEpoch.current) return;
      const apiError = asCoworkApiError(pickerError);
      setPickerOpening(false);
      setPickerFailure({
        message: pickerErrorMessage(apiError, "file"),
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
    if (!validCreatedMarkdownPath(normalizedPath)) {
      setError(
        mode === "create"
          ? "Use a safe .md or .markdown filename without reserved names or characters."
          : "Use a safe relative .md or .markdown location without reserved names or characters.",
      );
      return;
    }
    await bootstrapDocument(normalizedTitle, normalizedPath);
  };

  const submitImport = async (): Promise<void> => {
    if (
      selectedFilePath === null ||
      selectedImporter === null ||
      selectedSourceSha256 === null
    ) {
      setError("Choose a file to import.");
      return;
    }
    if (existingImportWarning !== null) {
      setError("Choose another file or open the existing Co-work copy.");
      return;
    }
    if (retiredImportConflict !== null) {
      setError("Choose another file to import.");
      return;
    }
    const issue = coworkProvenanceDeterminationIssue(authorshipAttestation);
    if (issue !== null) {
      setError(issue);
      return;
    }
    if (currentActorIdentity === null) {
      setError(
        "Co-work couldn’t bind this import to the current identity. Retry the identity check.",
      );
      return;
    }
    const converter = coworkFileConverter(selectedImporter.importerId);
    if (converter === null) {
      setError(
        "This Co-work app doesn’t include the converter version selected for that file. Refresh or update Co-work, then try again.",
      );
      return;
    }
    await bootstrapDocument(
      coworkImportedTitleFromPath(
        selectedFilePath,
        selectedImporter.suffixes,
      ),
      selectedFilePath,
      authorshipAttestation,
    );
  };

  const openExistingCopy = async (): Promise<void> => {
    const warning = existingImportWarning;
    if (warning === null || busy) return;
    setError(null);
    setStage("opening");
    try {
      await onOpened(warning.document);
      onClose();
    } catch (openError) {
      setError(
        coworkErrorMessage(
          asCoworkApiError(openError),
          "Co-work couldn’t open its existing copy.",
        ),
      );
      setStage("idle");
    }
  };

  useEffect(() => {
    if (mode !== "import" || initialFilePickerStarted.current) return;
    initialFilePickerStarted.current = true;
    if (filePickerAvailable) void chooseFile();
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
        <Dialog
          aria-labelledby="cowork-lifecycle-dialog-title"
          aria-busy={busy || undefined}
          className="wb-cowork-dialog__body"
        >
          <Heading id="cowork-lifecycle-dialog-title" slot="title">
            {mode === "create"
              ? "New document"
              : mode === "repair"
                ? "Repair document"
                : "From file"}
          </Heading>
          <p className="wb-cowork-dialog__folder">
            <strong title={folder.folderPath}>{folder.folderName}</strong>
          </p>
          {mode === "import" ? (
            <p>
              Import a Markdown file into Co-work. The selected file remains an
              unchanged source; Co-work keeps its own editable document.
            </p>
          ) : mode === "repair" ? (
            <InlineAlert tone="warning">
              Co-work will rebuild this document’s editing data from the current Markdown.
              The Markdown file itself will not be rewritten or deleted.
            </InlineAlert>
          ) : null}
          {mode === "import" && !filePickerAvailable ? (
            <InlineAlert id="cowork-file-picker-unavailable" tone="warning">
              File import isn’t available here.
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

          {mode === "import" ? (
            <>
              {selectedFilePath !== null ? (
                <>
                  <p className="wb-cowork-dialog__selection" title={selectedFilePath}>
                    <strong>{fileNameFromPath(selectedFilePath)}</strong>
                    <span>
                      {selectedFilePath}
                      {selectedImporter === null
                        ? ""
                        : ` · ${selectedImporter.displayName}`}
                    </span>
                  </p>
                  {existingImportWarning !== null ? (
                    <InlineAlert tone="warning" role="alert">
                      <strong>
                        {existingImportWarning.reason === "source_changed"
                          ? "This file has changed since it was imported."
                          : "Co-work can’t confirm which version of this file was imported."}
                      </strong>{" "}
                      Co-work has a separate managed copy. Opening that copy
                      will not refresh it from this file or change the file.
                    </InlineAlert>
                  ) : retiredImportConflict !== null ? (
                    <InlineAlert tone="warning" role="alert">
                      <strong>A Co-work copy of this file was retired.</strong>{" "}
                      The source file is unchanged. Co-work preserves the retired
                      document’s identity and history, so this path can’t be
                      imported again. Choose another file, or copy or rename this
                      file to import it as a new document.
                    </InlineAlert>
                  ) : (
                    <section
                      className="wb-cowork-dialog__step"
                      aria-labelledby="cowork-import-authorship-title"
                    >
                      <h3 id="cowork-import-authorship-title">
                        Where did this text come from?
                      </h3>
                      <p>
                        Record who wrote it and, when AI contributed, whether a
                        person reviewed it.
                      </p>
                      {currentActorIdentity === null ? (
                        identityFailure === null ? (
                          stage === "checking" ? (
                            <p role="status" aria-live="polite">
                              Checking the current identity…
                            </p>
                          ) : null
                        ) : (
                          <InlineAlert tone="danger" role="alert">
                            <span>{identityFailure}</span>
                            <Button
                              size="small"
                              onClick={() => void resolveImportActor()}
                              disabled={busy}
                            >
                              Retry identity
                            </Button>
                          </InlineAlert>
                        )
                      ) : (
                        <CoworkProvenanceForm
                          value={authorshipAttestation}
                          currentUserIdentity={currentActorIdentity}
                          disabled={busy}
                          idPrefix="cowork-file-import-provenance"
                          onChange={(value) => {
                            setAuthorshipAttestation(value);
                            setError(null);
                          }}
                        />
                      )}
                    </section>
                  )}
                </>
              ) : null}
              {pickerOpening ? (
                <p
                  role="status"
                  aria-live="polite"
                  className="wb-cowork-dialog__progress"
                >
                  <Spinner /> Opening file picker…
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
                        ? "Choose another location in this folder."
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
            <p
              role="status"
              aria-live="polite"
              className="wb-cowork-dialog__progress"
            >
              <Spinner /> {stageLabel[stage]}
            </p>
          ) : null}
          <div className="wb-cowork-dialog__actions">
            <Button onClick={closeDialog} disabled={busy}>Cancel</Button>
            {mode === "import" ? (
              pickerFailure !== null && pickerFailure.retryable ? (
                <Button variant="primary" onClick={() => void chooseFile()}>
                  Choose again
                </Button>
              ) : selectedFilePath !== null ? (
                <>
                  <Button onClick={() => void chooseFile()} disabled={busy}>
                    Choose another file
                  </Button>
                  {existingImportWarning !== null ? (
                    <Button
                      variant="primary"
                      onClick={() => void openExistingCopy()}
                      disabled={busy}
                    >
                      Open existing Co-work copy
                    </Button>
                  ) : retiredImportConflict !== null ? null : (
                    <Button
                      variant="primary"
                      onClick={() => void submitImport()}
                      disabled={
                        busy ||
                        selectedImporter === null ||
                        selectedSourceSha256 === null ||
                        currentActorIdentity === null ||
                        coworkProvenanceDeterminationIssue(
                          authorshipAttestation,
                        ) !== null
                      }
                    >
                      {error === null ? "Import" : "Try again"}
                    </Button>
                  )}
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
