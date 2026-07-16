import { ArrowLeft } from "@phosphor-icons/react/ArrowLeft";
import { FirstAid } from "@phosphor-icons/react/FirstAid";
import { MagnifyingGlass } from "@phosphor-icons/react/MagnifyingGlass";
import { Notebook } from "@phosphor-icons/react/Notebook";
import { PlugsConnected } from "@phosphor-icons/react/PlugsConnected";
import { TextAa } from "@phosphor-icons/react/TextAa";
import type { ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";

import { Button } from "../ui";
import type {
  SettingsNavigationGroup,
  SettingsAppCategory,
  SettingsPageContribution,
} from "./contracts";
import type { SettingsRegistry } from "./registry";

const GROUP_LABELS: Record<SettingsNavigationGroup, string> = {
  system: "System",
  apps: "Apps",
  connections: "Connections",
  status: "Setup & health",
};

const GROUP_ORDER: readonly SettingsNavigationGroup[] = [
  "system",
  "apps",
  "connections",
  "status",
];

const APP_CATEGORY_LABELS: Record<SettingsAppCategory, string> = {
  "built-in": "Built-in",
  personal: "Personal",
  community: "Community",
};

const APP_CATEGORY_ORDER: readonly SettingsAppCategory[] = [
  "built-in",
  "personal",
  "community",
];

function PageIcon({ page }: { readonly page: SettingsPageContribution }) {
  const icon: ReactNode =
    page.navigationGroup === "system" ? (
      <TextAa weight="duotone" />
    ) : page.navigationGroup === "apps" ? (
      <Notebook weight="duotone" />
    ) : page.navigationGroup === "connections" ? (
      <PlugsConnected weight="duotone" />
    ) : (
      <FirstAid weight="duotone" />
    );
  return <span aria-hidden="true">{icon}</span>;
}

function PageLink({ page }: { readonly page: SettingsPageContribution }) {
  const location = useLocation();
  return (
    <NavLink
      to={page.route}
      state={location.state}
      className={({ isActive }) =>
        `wb-settings-sidebar__link${isActive ? " is-active" : ""}`
      }
    >
      <PageIcon page={page} />
      {page.navigationLabel}
    </NavLink>
  );
}

function resultBreadcrumb(
  page: SettingsPageContribution,
  sectionLabel: string,
): string {
  if (page.navigationGroup === "apps" && page.appCategory) {
    return [
      GROUP_LABELS.apps,
      APP_CATEGORY_LABELS[page.appCategory],
      page.navigationLabel,
      sectionLabel,
    ].join(" · ");
  }
  return [GROUP_LABELS[page.navigationGroup], page.navigationLabel, sectionLabel].join(
    " · ",
  );
}

export interface SettingsSidebarProps {
  readonly registry: SettingsRegistry;
  readonly searchQuery: string;
  readonly returnLabel: string;
  onSearchQueryChange(value: string): void;
  onReturn(): void;
}

export function SettingsSidebar({
  registry,
  searchQuery,
  returnLabel,
  onSearchQueryChange,
  onReturn,
}: SettingsSidebarProps) {
  const location = useLocation();
  const results = registry.search(searchQuery);
  const pages = registry.listPages();

  return (
    <aside className="wb-settings-sidebar" aria-label="Settings navigation">
      <Button
        variant="ghost"
        size="small"
        className="wb-settings-sidebar__back"
        onClick={onReturn}
      >
        <ArrowLeft weight="bold" aria-hidden="true" />
        {returnLabel}
      </Button>

      <label className="wb-settings-search">
        <span className="wb-visually-hidden">Search all settings</span>
        <MagnifyingGlass weight="bold" aria-hidden="true" />
        <input
          type="search"
          value={searchQuery}
          placeholder="Search all settings…"
          onChange={(event) => onSearchQueryChange(event.target.value)}
        />
      </label>

      {searchQuery.trim() ? (
        <section className="wb-settings-search-results" aria-label="Search results">
          <p className="wb-settings-sidebar__heading">
            {results.length === 1 ? "1 result" : `${results.length} results`}
          </p>
          {results.length === 0 ? (
            <p className="wb-settings-search-results__empty">
              No matching settings.
            </p>
          ) : (
            <ul>
              {results.map((result) => (
                <li key={result.settingId}>
                  <Link
                    to={`${result.page.route}?setting=${encodeURIComponent(result.settingId)}`}
                    state={location.state}
                    onClick={() => onSearchQueryChange("")}
                  >
                    <strong>{result.definition.title}</strong>
                    <span>{resultBreadcrumb(result.page, result.section.label)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
      ) : (
        <nav aria-label="Settings pages" className="wb-settings-sidebar__groups">
          {GROUP_ORDER.map((group) => {
            const groupPages = pages.filter(
              (page) => page.navigationGroup === group,
            );
            if (groupPages.length === 0) return null;
            return (
              <section key={group} className="wb-settings-sidebar__group">
                <p className="wb-settings-sidebar__heading">{GROUP_LABELS[group]}</p>
                {group === "apps"
                  ? APP_CATEGORY_ORDER.map((category) => {
                      const categoryPages = groupPages.filter(
                        (page) => page.appCategory === category,
                      );
                      if (categoryPages.length === 0) return null;
                      return (
                        <div
                          key={category}
                          className="wb-settings-sidebar__app-category"
                        >
                          <p className="wb-settings-sidebar__subheading">
                            {APP_CATEGORY_LABELS[category]}
                          </p>
                          {categoryPages.map((page) => (
                            <PageLink key={page.pageId} page={page} />
                          ))}
                        </div>
                      );
                    })
                  : groupPages.map((page) => (
                      <PageLink key={page.pageId} page={page} />
                    ))}
              </section>
            );
          })}
        </nav>
      )}
    </aside>
  );
}
