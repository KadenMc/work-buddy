/**
 * The outer "From file" contract is format-neutral. The server owns file
 * admission and descriptor metadata; this module owns only versioned
 * byte-to-Co-work converters that ship with this frontend.
 */
import {
  bootstrapCoworkYdoc,
  type CoworkBootstrapYdocResult,
} from "./bootstrapCoworkYdoc";

export interface CoworkFileConverter {
  /** Exact, versioned importer identity supplied by the server descriptor. */
  readonly importerId: string;
  readonly convert: (source: Uint8Array) => Promise<CoworkBootstrapYdocResult>;
}

const markdownConverter: CoworkFileConverter = Object.freeze({
  importerId: "markdown/v1",
  convert: (source: Uint8Array) =>
    bootstrapCoworkYdoc(source, { allowNormalization: true }),
});

const convertersByImporterId: ReadonlyMap<string, CoworkFileConverter> =
  new Map([[markdownConverter.importerId, markdownConverter]]);

export const coworkFileConverter = (
  importerId: string,
): CoworkFileConverter | null =>
  convertersByImporterId.get(importerId) ?? null;

export const COWORK_IMPORTED_TITLE_MAX_CHARS = 240;

const cappedImportedTitle = (title: string): string => {
  const characters = Array.from(title);
  if (characters.length <= COWORK_IMPORTED_TITLE_MAX_CHARS) return title;
  const prefix = characters
    .slice(0, COWORK_IMPORTED_TITLE_MAX_CHARS - 1)
    .join("")
    .trimEnd();
  return `${prefix}…`;
};

/**
 * Derive a bounded display title from a server-admitted path. Format-specific
 * suffix metadata comes from the server descriptor, never from this registry.
 */
export const coworkImportedTitleFromPath = (
  path: string,
  suffixes: readonly string[],
): string => {
  const parts = path.replace(/\\/gu, "/").split("/");
  const fileName = parts[parts.length - 1] ?? "";
  const lowerFileName = fileName.toLocaleLowerCase("en-US");
  const matchingSuffix = [...suffixes]
    .sort((left, right) => right.length - left.length)
    .find((suffix) =>
      lowerFileName.endsWith(suffix.toLocaleLowerCase("en-US")),
    );
  const title =
    matchingSuffix === undefined
      ? fileName
      : fileName.slice(0, fileName.length - matchingSuffix.length);
  return cappedImportedTitle(title || "Untitled");
};
