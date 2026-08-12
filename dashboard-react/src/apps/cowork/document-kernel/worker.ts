import { createInterface } from "node:readline";

import {
  DOCUMENT_KERNEL_MAX_REQUEST_BYTES,
  type KernelRequest,
  type KernelResponse,
} from "./protocol";
import { executeKernelRequest } from "./runtime";

const binaryKeys = new Set([
  "sourceBase64",
  "snapshotBase64",
  "updatesBase64",
  "updateBase64",
]);

const decodeOperation = (request: KernelRequest): KernelRequest => {
  const operation = { ...request.operation } as Record<string, unknown>;
  for (const key of binaryKeys) {
    const value = operation[key];
    if (typeof value === "string") {
      operation[key] = new Uint8Array(Buffer.from(value, "base64"));
    } else if (Array.isArray(value)) {
      operation[key] = value.map((item) =>
        new Uint8Array(Buffer.from(String(item), "base64")),
      );
    }
  }
  return { ...request, operation } as KernelRequest;
};

const encodeBinary = (value: unknown): unknown => {
  if (value instanceof Uint8Array) return Buffer.from(value).toString("base64");
  if (Array.isArray(value)) return value.map(encodeBinary);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, item]) => [
        key,
        encodeBinary(item),
      ]),
    );
  }
  return value;
};

const contentFreeFailure = (requestId: string, operationKind: string): KernelResponse => ({
  protocol: "cowork-document-kernel/v1",
  runtimeVersion: "1.0.0",
  schemaVersion: "cowork-yjs/v1",
  requestId,
  operationKind: operationKind as KernelResponse["operationKind"],
  ok: false,
  error: { code: "invalid_request", retryable: false },
});

const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  let response: KernelResponse;
  if (Buffer.byteLength(line, "utf8") > DOCUMENT_KERNEL_MAX_REQUEST_BYTES) {
    response = contentFreeFailure("oversized-request", "health");
  } else {
    try {
      const parsed = JSON.parse(line) as KernelRequest;
      response = await executeKernelRequest(decodeOperation(parsed));
    } catch {
      response = contentFreeFailure("invalid-request", "health");
    }
  }
  process.stdout.write(`${JSON.stringify(encodeBinary(response))}\n`);
}
