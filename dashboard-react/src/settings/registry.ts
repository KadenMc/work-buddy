import type { SettingsPageId } from "../dashboard/contributions/contracts";
import type {
  ProjectedSettingsPage,
  SettingDefinition,
  SettingId,
  SettingPlacement,
  SettingsContribution,
  SettingsPageContribution,
  SettingsSearchResult,
} from "./contracts";

const ID_PATTERN = /^[a-z][a-z0-9]*(?:[._/-][a-z0-9][a-z0-9_-]*)+$/;
const SECTION_PATTERN = /^[a-z0-9][a-z0-9-]*$/;
const SEMANTIC_ROUTE_PATTERN =
  /^\/settings\/(?:(?:system|apps|connections)\/[a-z0-9][a-z0-9-]*|status)$/;
const LOCAL_TIME_PATTERN = /^(?:[01]\d|2[0-3]):[0-5]\d$/;

const key = (value: string): string => value;

function requireUnique(
  seen: Set<string>,
  value: string,
  kind: string,
): void {
  if (seen.has(value)) throw new Error(`Duplicate ${kind} ID: ${value}`);
  seen.add(value);
}

function validateDefault(definition: SettingDefinition): void {
  const value = definition.defaultValue;
  switch (definition.control.kind) {
    case "typography-scale":
      if (typeof value !== "string" || !definition.control.options.includes(value)) {
        throw new Error(`Invalid default for ${definition.settingId}`);
      }
      break;
    case "time":
      if (typeof value !== "string" || !LOCAL_TIME_PATTERN.test(value)) {
        throw new Error(`Invalid local-time default for ${definition.settingId}`);
      }
      break;
    case "switch":
      if (typeof value !== "boolean") {
        throw new Error(`Invalid switch default for ${definition.settingId}`);
      }
      break;
    case "select":
      if (
        typeof value !== "string" ||
        !definition.control.options.some((option) => option.value === value)
      ) {
        throw new Error(`Invalid select default for ${definition.settingId}`);
      }
      break;
  }
}

function validateCatalog(contributions: readonly SettingsContribution[]): void {
  const definitionIds = new Set<string>();
  const pageIds = new Set<string>();
  const placementIds = new Set<string>();
  const pageSettingPairs = new Set<string>();
  const routes = new Set<string>();
  const pages = new Map<string, SettingsPageContribution>();
  const definitions = new Map<string, SettingDefinition>();

  for (const contribution of contributions) {
    if (!contribution.sourceId.trim()) throw new Error("Settings sourceId is required");
    for (const definition of contribution.definitions) {
      requireUnique(definitionIds, definition.settingId, "setting");
      if (!ID_PATTERN.test(definition.settingId)) {
        throw new Error(`Invalid setting ID: ${definition.settingId}`);
      }
      if (!definition.settingId.startsWith(`${definition.ownerId}.`)) {
        throw new Error(
          `Setting ${definition.settingId} is outside owner namespace ${definition.ownerId}`,
        );
      }
      if (!definition.allowedScopes.includes(definition.defaultScope)) {
        throw new Error(`Default scope is not allowed for ${definition.settingId}`);
      }
      if (definition.sensitivity === "secret-reference") {
        throw new Error(
          `Secret-reference setting ${definition.settingId} requires a server-only renderer`,
        );
      }
      if (definition.visibility !== "frontend") {
        throw new Error(
          `Non-frontend setting ${definition.settingId} cannot enter the browser catalog`,
        );
      }
      validateDefault(definition);
      definitions.set(key(definition.settingId), definition);
    }

    for (const page of contribution.pages) {
      requireUnique(pageIds, page.pageId, "settings page");
      requireUnique(routes, page.route, "settings route");
      if (!ID_PATTERN.test(page.pageId)) {
        throw new Error(`Invalid settings page ID: ${page.pageId}`);
      }
      if (!SEMANTIC_ROUTE_PATTERN.test(page.route)) {
        throw new Error(
          `Settings route must use system/apps/connections/status semantics: ${page.route}`,
        );
      }
      if (page.navigationGroup === "apps" && page.appCategory === undefined) {
        throw new Error(`App settings page must declare appCategory: ${page.pageId}`);
      }
      if (page.navigationGroup !== "apps" && page.appCategory !== undefined) {
        throw new Error(
          `Only App settings pages may declare appCategory: ${page.pageId}`,
        );
      }
      if (page.route.startsWith("/settings/sections")) {
        throw new Error("Generic /settings/sections routes are not supported");
      }
      const sectionIds = new Set<string>();
      for (const section of page.sections) {
        requireUnique(sectionIds, section.sectionId, `section on ${page.pageId}`);
        if (!SECTION_PATTERN.test(section.sectionId)) {
          throw new Error(`Invalid settings section ID: ${section.sectionId}`);
        }
      }
      pages.set(key(page.pageId), page);
    }
  }

  for (const contribution of contributions) {
    for (const placement of contribution.placements) {
      requireUnique(placementIds, placement.placementId, "setting placement");
      requireUnique(
        pageSettingPairs,
        `${placement.pageId}\u0000${placement.settingId}`,
        "page setting placement",
      );
      if (!ID_PATTERN.test(placement.placementId)) {
        throw new Error(`Invalid setting placement ID: ${placement.placementId}`);
      }
      if (!definitions.has(key(placement.settingId))) {
        throw new Error(
          `Placement ${placement.placementId} references unknown setting ${placement.settingId}`,
        );
      }
      const page = pages.get(key(placement.pageId));
      if (!page) {
        throw new Error(
          `Placement ${placement.placementId} references unknown page ${placement.pageId}`,
        );
      }
      if (!page.sections.some((section) => section.sectionId === placement.sectionId)) {
        throw new Error(
          `Placement ${placement.placementId} references unknown section ${placement.sectionId}`,
        );
      }
    }
  }
}

function searchableText(
  definition: SettingDefinition,
  page: SettingsPageContribution,
  sectionLabel: string,
): string {
  return [
    definition.settingId,
    definition.title,
    definition.summary,
    definition.details,
    definition.ownerLabel,
    definition.provenance.label,
    page.label,
    page.context.label,
    sectionLabel,
    ...(definition.searchKeywords ?? []),
  ]
    .filter(Boolean)
    .join(" ")
    .toLocaleLowerCase();
}

/**
 * Immutable, candidate-validated registry. A failed merge never mutates the last
 * valid catalog, which is essential when an installed/server contribution is stale.
 */
export class SettingsRegistry {
  readonly #contributions: readonly SettingsContribution[];
  readonly #definitions: ReadonlyMap<string, SettingDefinition>;
  readonly #pages: ReadonlyMap<string, SettingsPageContribution>;
  readonly #placements: readonly SettingPlacement[];

  constructor(contributions: readonly SettingsContribution[] = []) {
    validateCatalog(contributions);
    this.#contributions = Object.freeze([...contributions]);
    this.#definitions = new Map(
      contributions.flatMap((contribution) =>
        contribution.definitions.map((definition) => [
          key(definition.settingId),
          definition,
        ] as const),
      ),
    );
    this.#pages = new Map(
      contributions.flatMap((contribution) =>
        contribution.pages.map((page) => [key(page.pageId), page] as const),
      ),
    );
    this.#placements = Object.freeze(
      contributions.flatMap((contribution) => contribution.placements),
    );
  }

  merge(contribution: SettingsContribution): SettingsRegistry {
    return new SettingsRegistry([...this.#contributions, contribution]);
  }

  /**
   * Same-origin server catalogs are authoritative for IDs they publish. The
   * complete overlaid candidate is validated before publication, so a malformed
   * response cannot partially replace native fallbacks.
   */
  mergeAuthoritative(contribution: SettingsContribution): SettingsRegistry {
    const definitionIds = new Set(
      contribution.definitions.map((definition) => key(definition.settingId)),
    );
    const pageIds = new Set(contribution.pages.map((page) => key(page.pageId)));
    const placementIds = new Set(
      contribution.placements.map((placement) => key(placement.placementId)),
    );
    const retained = this.#contributions.map((existing) => ({
      ...existing,
      definitions: existing.definitions.filter(
        (definition) => !definitionIds.has(key(definition.settingId)),
      ),
      pages: existing.pages.filter((page) => !pageIds.has(key(page.pageId))),
      placements: existing.placements.filter(
        (placement) =>
          !placementIds.has(key(placement.placementId)) &&
          !definitionIds.has(key(placement.settingId)) &&
          !pageIds.has(key(placement.pageId)),
      ),
    }));
    return new SettingsRegistry([...retained, contribution]);
  }

  listPages(): readonly SettingsPageContribution[] {
    return [...this.#pages.values()].sort(
      (left, right) =>
        left.navigationOrder - right.navigationOrder ||
        left.navigationLabel.localeCompare(right.navigationLabel),
    );
  }

  listDefinitions(): readonly SettingDefinition[] {
    return [...this.#definitions.values()];
  }

  getPage(pageId: SettingsPageId): SettingsPageContribution | undefined {
    return this.#pages.get(key(pageId));
  }

  getPageByRoute(route: string): SettingsPageContribution | undefined {
    return [...this.#pages.values()].find((page) => page.route === route);
  }

  getDefinition(settingId: SettingId): SettingDefinition | undefined {
    return this.#definitions.get(key(settingId));
  }

  projectPage(pageId: SettingsPageId): ProjectedSettingsPage | undefined {
    const page = this.getPage(pageId);
    if (!page) return undefined;
    return {
      page,
      sections: [...page.sections]
        .sort((left, right) => left.order - right.order)
        .map((section) => ({
          definition: section,
          settings: this.#placements
            .filter(
              (placement) =>
                placement.pageId === pageId &&
                placement.sectionId === section.sectionId,
            )
            .sort((left, right) => left.order - right.order)
            .map((placement) => ({
              placement,
              definition: this.#definitions.get(key(placement.settingId))!,
            })),
        })),
    };
  }

  /**
   * Filter one already-loaded page projection. This is the reliable, offline-safe
   * Settings search path; optional semantic ranking may enhance discovery later,
   * but it must never be required to filter the page currently in front of a user.
   */
  searchPage(
    pageId: SettingsPageId,
    query: string,
  ): ProjectedSettingsPage | undefined {
    const projection = this.projectPage(pageId);
    if (!projection) return undefined;
    const terms = query
      .trim()
      .toLocaleLowerCase()
      .split(/\s+/)
      .filter(Boolean);
    if (terms.length === 0) return projection;

    return {
      page: projection.page,
      sections: projection.sections
        .map((section) => ({
          ...section,
          settings: section.settings.filter(({ definition, placement }) => {
            const haystack = [
              searchableText(
                definition,
                projection.page,
                section.definition.label,
              ),
              section.definition.description,
              placement.contextualSummary,
            ]
              .filter(Boolean)
              .join(" ")
              .toLocaleLowerCase();
            return terms.every((term) => haystack.includes(term));
          }),
        }))
        .filter((section) => section.settings.length > 0),
    };
  }

  search(query: string): readonly SettingsSearchResult[] {
    const terms = query
      .trim()
      .toLocaleLowerCase()
      .split(/\s+/)
      .filter(Boolean);
    if (terms.length === 0) return [];

    const candidates = this.#placements.flatMap((placement) => {
      const page = this.#pages.get(key(placement.pageId));
      const definition = this.#definitions.get(key(placement.settingId));
      const section = page?.sections.find(
        (candidate) => candidate.sectionId === placement.sectionId,
      );
      if (!page || !definition || !section) return [];
      const haystack = searchableText(definition, page, section.label);
      return terms.every((term) => haystack.includes(term))
        ? [{ settingId: definition.settingId, definition, placement, page, section }]
        : [];
    });

    const deduplicated = new Map<string, SettingsSearchResult>();
    for (const candidate of candidates) {
      const previous = deduplicated.get(key(candidate.settingId));
      if (!previous || candidate.placement.preferredForSearch) {
        deduplicated.set(key(candidate.settingId), candidate);
      }
    }
    return [...deduplicated.values()].sort((left, right) =>
      left.definition.title.localeCompare(right.definition.title),
    );
  }
}
