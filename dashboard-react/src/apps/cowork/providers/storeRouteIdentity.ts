import type {
  CoworkDocumentSummary,
  CoworkFolderSummary,
  CoworkRouteTarget,
} from "../contracts";

export const COWORK_STORE_URL_PREFIX_LENGTH = 8;
export const COWORK_DOCUMENT_URL_PREFIX_LENGTH = 8;

const uniqueIds = <T>(
  entries: readonly T[],
  identify: (entry: T) => string,
): readonly string[] => [...new Set(entries.map(identify))];

const resolveUrlId = (
  urlId: string,
  ids: readonly string[],
  prefixLength: number,
): string | null => {
  if (ids.includes(urlId)) return urlId;
  if (urlId.length !== prefixLength) return null;

  const matches = ids.filter((id) => id.startsWith(urlId));
  return matches.length === 1 ? matches[0] : null;
};

const urlIdFor = (
  id: string,
  ids: readonly string[],
  prefixLength: number,
): string => {
  if (id.length <= prefixLength || !ids.includes(id)) return id;

  const prefix = id.slice(0, prefixLength);
  const matches = ids.filter((candidate) => candidate.startsWith(prefix));
  return matches.length === 1 ? prefix : id;
};

const uniqueStoreIds = (
  folders: readonly Pick<CoworkFolderSummary, "storeId">[],
): readonly string[] => uniqueIds(folders, (folder) => folder.storeId);

const uniqueDocumentIds = (
  documents: readonly Pick<CoworkDocumentSummary, "documentId">[],
): readonly string[] => uniqueIds(documents, (document) => document.documentId);

/**
 * Resolve a URL-safe store identity against the authoritative Folder catalog.
 * Exact IDs always win, including legacy IDs shorter than the URL prefix length.
 * A prefix is accepted only when it is exactly eight characters and uniquely identifies a Folder.
 */
export const resolveCoworkStoreUrlId = (
  urlStoreId: string,
  folders: readonly Pick<CoworkFolderSummary, "storeId">[],
): string | null => {
  return resolveUrlId(
    urlStoreId,
    uniqueStoreIds(folders),
    COWORK_STORE_URL_PREFIX_LENGTH,
  );
};

/**
 * Prefer an eight-character URL prefix only when the full ID is present in the
 * current Folder catalog and that prefix is unique. Otherwise retain the full ID.
 */
export const coworkStoreUrlId = (
  storeId: string,
  folders: readonly Pick<CoworkFolderSummary, "storeId">[],
): string => {
  return urlIdFor(
    storeId,
    uniqueStoreIds(folders),
    COWORK_STORE_URL_PREFIX_LENGTH,
  );
};

/**
 * Resolve a URL-facing document identity inside one authoritative Folder catalog.
 * Document prefixes are store-scoped: an ID in another Folder cannot make this
 * Folder's URL ambiguous. Exact IDs always win over prefix interpretation.
 */
export const resolveCoworkDocumentUrlId = (
  urlDocumentId: string,
  documents: readonly Pick<CoworkDocumentSummary, "documentId">[],
): string | null =>
  resolveUrlId(
    urlDocumentId,
    uniqueDocumentIds(documents),
    COWORK_DOCUMENT_URL_PREFIX_LENGTH,
  );

/**
 * Prefer a unique store-scoped eight-character document prefix. Unknown IDs and
 * collisions remain full so presentation aliases never replace canonical identity.
 */
export const coworkDocumentUrlId = (
  documentId: string,
  documents: readonly Pick<CoworkDocumentSummary, "documentId">[],
): string =>
  urlIdFor(
    documentId,
    uniqueDocumentIds(documents),
    COWORK_DOCUMENT_URL_PREFIX_LENGTH,
  );

/** Replace only the URL-facing store identity; all provider state remains canonical. */
export const resolveCoworkRouteStoreId = (
  route: CoworkRouteTarget,
  folders: readonly Pick<CoworkFolderSummary, "storeId">[],
): CoworkRouteTarget => {
  if (route.kind === "scratch" || route.kind === "unavailable") return route;
  if (route.storeId === null) return route;

  const storeId = resolveCoworkStoreUrlId(route.storeId, folders);
  if (storeId === null || storeId === route.storeId) return route;
  return { ...route, storeId };
};
