import { sha256Hex } from "../persistence/hashing";

import type { CoworkDocumentTargetReference } from "./contracts";

type CanonicalJson =
  | null
  | boolean
  | number
  | string
  | readonly CanonicalJson[]
  | { readonly [key: string]: CanonicalJson };

const normalizeText = (value: string): string =>
  value.trim().split(/\s+/u).filter(Boolean).join(" ");

const canonicalize = (value: CanonicalJson): CanonicalJson => {
  if (typeof value === "string") return normalizeText(value);
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value !== null && typeof value === "object") {
    const record = value as { readonly [key: string]: CanonicalJson };
    return Object.fromEntries(
      Object.keys(record)
        .sort()
        .map((key) => [key, canonicalize(record[key])]),
    );
  }
  return value;
};

/**
 * Mirrors the server's trust-bound v1 target-reference identity. Presentation
 * metadata and timestamps are deliberately excluded; character granularity
 * is included while legacy block references preserve their historical hash.
 */
export const coworkTargetReferenceIdentitySha256 = async (
  reference: CoworkDocumentTargetReference,
): Promise<string> => {
  const identity: Record<string, CanonicalJson> = {
    schema: "wb.cowork.document-target/v1",
    storeId: reference.storeId,
    documentId: reference.documentId,
    kind: "text_range",
    relative: {
      startBase64: reference.relative.startBase64,
      endBase64: reference.relative.endBase64,
    },
    quote: {
      exact: reference.quote.exact,
      prefix: reference.quote.prefix,
      suffix: reference.quote.suffix,
    },
  };
  if (reference.granularity === "character") {
    identity.granularity = "character";
  }
  return sha256Hex(
    new TextEncoder().encode(JSON.stringify(canonicalize(identity))),
  );
};
