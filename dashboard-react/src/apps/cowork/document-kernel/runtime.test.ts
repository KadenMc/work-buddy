import { describe, expect, it } from "vitest";
import * as Y from "yjs";

import { sha256Hex } from "../persistence/hashing";
import {
  bootstrapMarkdownYdoc,
  projectYdocMarkdown,
} from "./markdown";
import {
  DOCUMENT_KERNEL_PROTOCOL,
  DOCUMENT_KERNEL_RUNTIME_VERSION,
  type KernelRequest,
} from "./protocol";
import { COWORK_DOCUMENT_SCHEMA } from "./schema";
import { executeKernelRequest, structuredHeadSha256 } from "./runtime";

const encoder = new TextEncoder();

const request = (
  requestId: string,
  operation: KernelRequest["operation"],
): KernelRequest => ({
  protocol: DOCUMENT_KERNEL_PROTOCOL,
  runtimeVersion: DOCUMENT_KERNEL_RUNTIME_VERSION,
  schemaVersion: COWORK_DOCUMENT_SCHEMA,
  requestId,
  deadlineMs: Date.now() + 10_000,
  operation,
});

describe("shared structured-document kernel", () => {
  it("matches browser bootstrap/projection for complex Markdown", async () => {
    const markdown = "# Café 🧭\n\nA **bold** and [linked](https://example.com) line.\n\n- [x] Done\n";
    const source = encoder.encode(markdown);
    const sourceSha = await sha256Hex(source);
    const response = await executeKernelRequest(
      request("runtime_parity_01", {
        kind: "bootstrap_markdown",
        sourceBase64: source as unknown as string,
        sourceSha256: sourceSha,
        newlineStyle: "lf",
        utf8Bom: false,
        trailingNewlineCount: 1,
      }),
    );
    expect(response.ok).toBe(true);
    const runtimeProjection = response.result?.projection as Uint8Array;

    const browserDocument = bootstrapMarkdownYdoc(
      markdown,
      {
        newlineStyle: "lf",
        utf8Bom: false,
        trailingNewlineCount: 1,
      },
      (path, node) => `fixture-${node.type}-${path.join("-")}`,
      { source_sha256: sourceSha },
    );
    expect(new TextDecoder().decode(runtimeProjection)).toBe(
      projectYdocMarkdown(browserDocument),
    );
    browserDocument.destroy();
  });

  it("supports whole-document source replacement, text replacement, and projection", async () => {
    const first = encoder.encode("Alpha beta.\n");
    const boot = await executeKernelRequest(
      request("runtime_mutation_boot", {
        kind: "bootstrap_markdown",
        sourceBase64: first as unknown as string,
        sourceSha256: await sha256Hex(first),
        newlineStyle: "lf",
        utf8Bom: false,
        trailingNewlineCount: 1,
      }),
    );
    expect(boot.ok).toBe(true);
    const initialSnapshot = boot.result?.snapshot as Uint8Array;

    const second = encoder.encode("Gamma delta.\n");
    const whole = await executeKernelRequest(
      request("runtime_mutation_whole", {
        kind: "apply_source_markdown",
        snapshotBase64: initialSnapshot as unknown as string,
        updatesBase64: [],
        expectedBaseStructuredHeadSha256: await structuredHeadSha256(
          initialSnapshot,
          [],
        ),
        sourceBase64: second as unknown as string,
        sourceSha256: await sha256Hex(second),
        newlineStyle: "lf",
        utf8Bom: false,
        trailingNewlineCount: 1,
      }),
    );
    expect(new TextDecoder().decode(whole.result?.projection as Uint8Array)).toBe(
      "Gamma delta.\n",
    );
    const wholeSnapshot = whole.result?.snapshot as Uint8Array;
    const copiedText = "Omega";
    const text = await executeKernelRequest(
      request("runtime_mutation_text", {
        kind: "replace_text",
        snapshotBase64: wholeSnapshot as unknown as string,
        updatesBase64: [],
        expectedBaseStructuredHeadSha256: await structuredHeadSha256(
          wholeSnapshot,
          [],
        ),
        selector: {
          kind: "prosemirror_text/v1",
          from: 1,
          to: 6,
          expectedText: "Gamma",
        },
        copiedText,
        copiedTextSha256: await sha256Hex(encoder.encode(copiedText)),
      }),
    );
    expect(text.ok).toBe(true);
    expect(new TextDecoder().decode(text.result?.projection as Uint8Array)).toBe(
      "Omega delta.\n",
    );

    const candidateUpdate = text.result?.update as Uint8Array;
    const validated = await executeKernelRequest(
      request("runtime_validate_update", {
        kind: "validate_yjs_update",
        snapshotBase64: wholeSnapshot as unknown as string,
        updatesBase64: [],
        expectedBaseStructuredHeadSha256: await structuredHeadSha256(
          wholeSnapshot,
          [],
        ),
        updateBase64: candidateUpdate as unknown as string,
        expectedResultStructuredHeadSha256: await structuredHeadSha256(
          wholeSnapshot,
          [candidateUpdate],
        ),
      }),
    );
    expect(validated.ok).toBe(true);
    expect(new TextDecoder().decode(validated.result?.projection as Uint8Array)).toBe(
      "Omega delta.\n",
    );

    const textSnapshot = text.result?.snapshot as Uint8Array;
    const projected = await executeKernelRequest(
      request("runtime_projection_02", {
        kind: "project_markdown",
        snapshotBase64: textSnapshot as unknown as string,
        updatesBase64: [],
        expectedBaseStructuredHeadSha256: await structuredHeadSha256(
          textSnapshot,
          [],
        ),
      }),
    );
    expect(projected.result?.projectionSha256).toBe(
      await sha256Hex(projected.result?.projection as Uint8Array),
    );

    // A plain Y.Doc can consume the kernel snapshot: no browser view state is
    // hidden in the runtime result.
    const document = new Y.Doc();
    Y.applyUpdate(document, textSnapshot);
    expect(projectYdocMarkdown(document)).toBe("Omega delta.\n");
    document.destroy();
  });
});
