import type { CoworkFolderSummary, CoworkRouteTarget } from "../contracts";

export const COWORK_STORE_URL_PREFIX_LENGTH = 8;

const uniqueStoreIds = (
  folders: readonly Pick<CoworkFolderSummary, "storeId">[],
): readonly string[] => [...new Set(folders.map((folder) => folder.storeId))];

/**
 * Resolve a URL-safe store identity against the authoritative Folder catalog.
 * Exact IDs always win, including legacy IDs shorter than the URL prefix length.
 * A prefix is accepted only when it is exactly eight characters and uniquely identifies a Folder.
 */
export const resolveCoworkStoreUrlId = (
  urlStoreId: string,
  folders: readonly Pick<CoworkFolderSummary, "storeId">[],
): string | null => {
  const storeIds = uniqueStoreIds(folders);
  if (storeIds.includes(urlStoreId)) return urlStoreId;
  if (urlStoreId.length !== COWORK_STORE_URL_PREFIX_LENGTH) return null;

  const matches = storeIds.filter((storeId) => storeId.startsWith(urlStoreId));
  return matches.length === 1 ? matches[0] : null;
};

/**
 * Prefer an eight-character URL prefix only when the full ID is present in the
 * current Folder catalog and that prefix is unique. Otherwise retain the full ID.
 */
export const coworkStoreUrlId = (
  storeId: string,
  folders: readonly Pick<CoworkFolderSummary, "storeId">[],
): string => {
  if (storeId.length <= COWORK_STORE_URL_PREFIX_LENGTH) return storeId;

  const storeIds = uniqueStoreIds(folders);
  if (!storeIds.includes(storeId)) return storeId;
  const prefix = storeId.slice(0, COWORK_STORE_URL_PREFIX_LENGTH);
  const matches = storeIds.filter((candidate) => candidate.startsWith(prefix));
  return matches.length === 1 ? prefix : storeId;
};

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
