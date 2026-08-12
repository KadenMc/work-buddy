import { EditorState } from "@tiptap/pm/state";
import * as Y from "yjs";
import {
  initProseMirrorDoc,
  updateYFragment,
} from "@tiptap/y-tiptap";

import { sha256Hex } from "../persistence/hashing";
import {
  assignDocumentBlockIds,
  bootstrapMarkdownYdoc,
  createDocumentMarkdownManager,
  createDocumentSchema,
  projectYdocMarkdown,
  writeFidelityToYdoc,
} from "./markdown";
import {
  COWORK_DOCUMENT_SCHEMA,
  COWORK_FRAGMENT_FIELD,
} from "./schema";
import {
  DOCUMENT_KERNEL_MAX_SEGMENT_BYTES,
  DOCUMENT_KERNEL_PROTOCOL,
  DOCUMENT_KERNEL_RUNTIME_VERSION,
  DocumentKernelError,
  type KernelRequest,
  type KernelResponse,
} from "./protocol";

const encoder = new TextEncoder();
const fatalDecoder = new TextDecoder("utf-8", { fatal: true });
const HEAD_DOMAIN = encoder.encode("cowork-yjs-structured-head/v1\0");

const assertDigest = (value: unknown, code = "invalid_digest"): string => {
  if (typeof value !== "string" || !/^[0-9a-f]{64}$/u.test(value)) {
    throw new DocumentKernelError(code);
  }
  return value;
};

const frame = (value: Uint8Array): Uint8Array => {
  const result = new Uint8Array(4 + value.length);
  new DataView(result.buffer).setUint32(0, value.length, false);
  result.set(value, 4);
  return result;
};

const concatenate = (values: readonly Uint8Array[]): Uint8Array => {
  const size = values.reduce((total, value) => total + value.length, 0);
  const result = new Uint8Array(size);
  let offset = 0;
  for (const value of values) {
    result.set(value, offset);
    offset += value.length;
  }
  return result;
};

export const structuredHeadSha256 = async (
  snapshot: Uint8Array,
  updates: readonly Uint8Array[],
): Promise<string> =>
  sha256Hex(concatenate([HEAD_DOMAIN, frame(snapshot), ...updates.map(frame)]));

const stableBlockId = (
  seed: string,
  path: readonly number[],
  type: string | undefined,
): string => {
  let hash = 2166136261;
  const identity = `${type ?? "node"}:${path.join(".")}`;
  for (let index = 0; index < identity.length; index += 1) {
    hash ^= identity.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `wb-${seed.slice(0, 16)}-${(hash >>> 0).toString(16).padStart(8, "0")}`;
};

const ensureSegment = (value: Uint8Array): Uint8Array => {
  if (value.length > DOCUMENT_KERNEL_MAX_SEGMENT_BYTES) {
    throw new DocumentKernelError("segment_too_large");
  }
  return value;
};

const loadYdoc = (
  snapshot: Uint8Array,
  updates: readonly Uint8Array[],
): Y.Doc => {
  ensureSegment(snapshot);
  const document = new Y.Doc();
  Y.applyUpdate(document, snapshot);
  for (const update of updates) Y.applyUpdate(document, ensureSegment(update));
  return document;
};

const applyProseMirrorResult = (
  document: Y.Doc,
  resultDoc: ReturnType<typeof createDocumentSchema>["topNodeType"] extends never
    ? never
    : import("@tiptap/pm/model").Node,
  updateMetadata?: () => void,
): Uint8Array => {
  const stateVector = Y.encodeStateVector(document);
  const fragment = document.getXmlFragment(COWORK_FRAGMENT_FIELD);
  const current = initProseMirrorDoc(fragment, resultDoc.type.schema);
  updateYFragment(document, fragment, resultDoc, current.meta);
  updateMetadata?.();
  return Y.encodeStateAsUpdate(document, stateVector);
};

const canonicalJson = (value: unknown): string => {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  return `{${Object.entries(value as Record<string, unknown>)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`)
    .join(",")}}`;
};

const baseResult = async (
  request: KernelRequest,
  document: Y.Doc,
  update: Uint8Array,
  baseHead: string,
  extra: Readonly<Record<string, unknown>>,
): Promise<Readonly<Record<string, unknown>>> => {
  const snapshot = Y.encodeStateAsUpdate(document);
  const projection = encoder.encode(projectYdocMarkdown(document));
  const manifest = {
    protocol: DOCUMENT_KERNEL_PROTOCOL,
    runtimeVersion: DOCUMENT_KERNEL_RUNTIME_VERSION,
    schemaVersion: COWORK_DOCUMENT_SCHEMA,
    requestId: request.requestId,
    operationKind: request.operation.kind,
    baseStructuredHeadSha256: baseHead,
    resultSnapshotSha256: await sha256Hex(snapshot),
    resultUpdateSha256: await sha256Hex(update),
    resultProjectionSha256: await sha256Hex(projection),
    ...extra,
  };
  return {
    ...manifest,
    snapshot,
    update,
    projection,
    operationManifestSha256: await sha256Hex(encoder.encode(canonicalJson(manifest))),
  };
};

const validateRequest = (request: KernelRequest): void => {
  if (request.protocol !== DOCUMENT_KERNEL_PROTOCOL) {
    throw new DocumentKernelError("protocol_version_mismatch");
  }
  if (request.runtimeVersion !== DOCUMENT_KERNEL_RUNTIME_VERSION) {
    throw new DocumentKernelError("runtime_version_mismatch");
  }
  if (request.schemaVersion !== COWORK_DOCUMENT_SCHEMA) {
    throw new DocumentKernelError("schema_version_mismatch");
  }
  if (!/^[A-Za-z0-9_-]{8,128}$/u.test(request.requestId)) {
    throw new DocumentKernelError("invalid_request_id");
  }
  if (!Number.isSafeInteger(request.deadlineMs) || Date.now() > request.deadlineMs) {
    throw new DocumentKernelError("deadline_expired", true);
  }
};

/** Binary fields are decoded/encoded by the narrow Node transport wrapper. */
export const executeKernelRequest = async (
  request: KernelRequest,
): Promise<KernelResponse> => {
  try {
    validateRequest(request);
    const operation = request.operation;
    if (operation.kind === "health") {
      return {
        protocol: DOCUMENT_KERNEL_PROTOCOL,
        runtimeVersion: DOCUMENT_KERNEL_RUNTIME_VERSION,
        schemaVersion: COWORK_DOCUMENT_SCHEMA,
        requestId: request.requestId,
        operationKind: operation.kind,
        ok: true,
        result: { status: "ready", domPresent: typeof globalThis.document !== "undefined" },
      };
    }

    if (operation.kind === "bootstrap_markdown") {
      const source = ensureSegment(operation.sourceBase64 as unknown as Uint8Array);
      if ((await sha256Hex(source)) !== assertDigest(operation.sourceSha256)) {
        throw new DocumentKernelError("source_hash_mismatch");
      }
      let markdown: string;
      try {
        markdown = fatalDecoder.decode(source);
      } catch {
        throw new DocumentKernelError("invalid_utf8");
      }
      const document = bootstrapMarkdownYdoc(
        markdown,
        {
          newlineStyle: operation.newlineStyle,
          utf8Bom: operation.utf8Bom,
          trailingNewlineCount: operation.trailingNewlineCount,
        },
        (path, node) => stableBlockId(operation.sourceSha256, path, node.type),
        { source_sha256: operation.sourceSha256 },
      );
      const update = Y.encodeStateAsUpdate(document);
      const result = await baseResult(request, document, update, "0".repeat(64), {
        exactCopiedTextSha256: operation.sourceSha256,
        assurance: {
          structure: "document_kernel_verified",
          persistence: "not_checked",
        },
      });
      document.destroy();
      return {
        protocol: DOCUMENT_KERNEL_PROTOCOL,
        runtimeVersion: DOCUMENT_KERNEL_RUNTIME_VERSION,
        schemaVersion: COWORK_DOCUMENT_SCHEMA,
        requestId: request.requestId,
        operationKind: operation.kind,
        ok: true,
        result,
      };
    }

    const snapshot = ensureSegment(operation.snapshotBase64 as unknown as Uint8Array);
    const updates = operation.updatesBase64 as unknown as readonly Uint8Array[];
    const baseHead = await structuredHeadSha256(snapshot, updates);
    if (baseHead !== assertDigest(operation.expectedBaseStructuredHeadSha256)) {
      throw new DocumentKernelError("base_head_mismatch", true);
    }
    const document = loadYdoc(snapshot, updates);

    if (operation.kind === "project_markdown") {
      const projection = encoder.encode(projectYdocMarkdown(document));
      const result = {
        baseStructuredHeadSha256: baseHead,
        projection,
        projectionSha256: await sha256Hex(projection),
      };
      document.destroy();
      return {
        protocol: DOCUMENT_KERNEL_PROTOCOL,
        runtimeVersion: DOCUMENT_KERNEL_RUNTIME_VERSION,
        schemaVersion: COWORK_DOCUMENT_SCHEMA,
        requestId: request.requestId,
        operationKind: operation.kind,
        ok: true,
        result,
      };
    }

    if (operation.kind === "validate_yjs_update") {
      const update = ensureSegment(operation.updateBase64 as unknown as Uint8Array);
      const resultHead = await structuredHeadSha256(snapshot, [...updates, update]);
      if (
        resultHead !==
        assertDigest(
          operation.expectedResultStructuredHeadSha256,
          "invalid_result_head",
        )
      ) {
        throw new DocumentKernelError("result_head_mismatch", true);
      }
      Y.applyUpdate(document, update);
      // Parse the resulting shared fragment against the canonical schema
      // before accepting it as a durable editor update.
      initProseMirrorDoc(
        document.getXmlFragment(COWORK_FRAGMENT_FIELD),
        createDocumentSchema(),
      );
      const projection = encoder.encode(projectYdocMarkdown(document));
      const manifest = {
        protocol: DOCUMENT_KERNEL_PROTOCOL,
        runtimeVersion: DOCUMENT_KERNEL_RUNTIME_VERSION,
        schemaVersion: COWORK_DOCUMENT_SCHEMA,
        requestId: request.requestId,
        operationKind: operation.kind,
        baseStructuredHeadSha256: baseHead,
        resultSnapshotSha256: await sha256Hex(snapshot),
        resultStructuredHeadSha256: resultHead,
        resultUpdateSha256: await sha256Hex(update),
        resultProjectionSha256: await sha256Hex(projection),
        assurance: { structure: "document_kernel_verified", inputter: "surface_attested" },
      };
      document.destroy();
      return {
        protocol: DOCUMENT_KERNEL_PROTOCOL,
        runtimeVersion: DOCUMENT_KERNEL_RUNTIME_VERSION,
        schemaVersion: COWORK_DOCUMENT_SCHEMA,
        requestId: request.requestId,
        operationKind: operation.kind,
        ok: true,
        result: {
          ...manifest,
          update,
          projection,
          operationManifestSha256: await sha256Hex(
            encoder.encode(canonicalJson(manifest)),
          ),
        },
      };
    }

    const schema = createDocumentSchema();
    const current = initProseMirrorDoc(
      document.getXmlFragment(COWORK_FRAGMENT_FIELD),
      schema,
    ).doc;
    let resultDoc: import("@tiptap/pm/model").Node;
    let extra: Readonly<Record<string, unknown>>;
    let metadata: (() => void) | undefined;

    if (operation.kind === "apply_source_markdown") {
      const source = ensureSegment(operation.sourceBase64 as unknown as Uint8Array);
      if ((await sha256Hex(source)) !== assertDigest(operation.sourceSha256)) {
        throw new DocumentKernelError("source_hash_mismatch");
      }
      let markdown: string;
      try {
        markdown = fatalDecoder.decode(source);
      } catch {
        throw new DocumentKernelError("invalid_utf8");
      }
      const manager = createDocumentMarkdownManager();
      const imported = manager.parse(markdown);
      const identified = assignDocumentBlockIds(
        imported,
        (path, node) => stableBlockId(operation.sourceSha256, path, node.type),
      );
      resultDoc = schema.nodeFromJSON(identified);
      metadata = () =>
        writeFidelityToYdoc(
          document,
          {
            newlineStyle: operation.newlineStyle,
            utf8Bom: operation.utf8Bom,
            trailingNewlineCount: operation.trailingNewlineCount,
            frontmatter: null,
          },
          { source_sha256: operation.sourceSha256 },
        );
      extra = {
        selector: { kind: "whole_document/v1" },
        exactCopiedTextSha256: operation.sourceSha256,
        exactBeforeTextSha256: await sha256Hex(encoder.encode(current.textContent)),
        assurance: { exactCopy: "document_kernel_verified" },
      };
    } else {
      const copiedDigest = await sha256Hex(encoder.encode(operation.copiedText));
      if (copiedDigest !== assertDigest(operation.copiedTextSha256)) {
        throw new DocumentKernelError("copied_text_hash_mismatch");
      }
      const { from, to, expectedText } = operation.selector;
      if (
        operation.selector.kind !== "prosemirror_text/v1" ||
        !Number.isSafeInteger(from) ||
        !Number.isSafeInteger(to) ||
        from < 0 ||
        to < from ||
        to > current.content.size
      ) {
        throw new DocumentKernelError("invalid_selector");
      }
      const actual = current.textBetween(from, to, "\n", "\ufffc");
      if (actual !== expectedText) throw new DocumentKernelError("selector_mismatch", true);
      resultDoc = EditorState.create({ schema, doc: current }).tr
        .insertText(operation.copiedText, from, to).doc;
      extra = {
        selector: operation.selector,
        exactCopiedTextSha256: operation.copiedTextSha256,
        exactBeforeTextSha256: await sha256Hex(encoder.encode(actual)),
        assurance: { exactCopy: "document_kernel_verified" },
      };
    }

    const update = applyProseMirrorResult(document, resultDoc, metadata);
    const result = await baseResult(request, document, update, baseHead, extra);
    document.destroy();
    return {
      protocol: DOCUMENT_KERNEL_PROTOCOL,
      runtimeVersion: DOCUMENT_KERNEL_RUNTIME_VERSION,
      schemaVersion: COWORK_DOCUMENT_SCHEMA,
      requestId: request.requestId,
      operationKind: operation.kind,
      ok: true,
      result,
    };
  } catch (error) {
    const failure =
      error instanceof DocumentKernelError
        ? error
        : new DocumentKernelError("kernel_operation_failed", true);
    return {
      protocol: DOCUMENT_KERNEL_PROTOCOL,
      runtimeVersion: DOCUMENT_KERNEL_RUNTIME_VERSION,
      schemaVersion: COWORK_DOCUMENT_SCHEMA,
      requestId:
        typeof request?.requestId === "string" ? request.requestId : "invalid-request",
      operationKind: request?.operation?.kind ?? "health",
      ok: false,
      error: { code: failure.code, retryable: failure.retryable },
    };
  }
};
