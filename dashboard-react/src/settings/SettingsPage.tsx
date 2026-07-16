import { MagnifyingGlass } from "@phosphor-icons/react/MagnifyingGlass";
import { X } from "@phosphor-icons/react/X";
import type { CSSProperties, ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import {
  DEFAULT_TYPOGRAPHY_SCALE,
  TYPOGRAPHY_SCALE_OPTIONS,
  useTypographyScale,
} from "../theme/TypographyScaleProvider";
import { ControlStatusPage } from "../system-status";
import { Button, IconButton, InlineAlert } from "../ui";
import type {
  EffectiveSettingValue,
  ProjectedSetting,
  SettingDefinition,
  SettingId,
  SettingsPageContribution,
} from "./contracts";
import { JOURNAL_DAY_BOUNDARY_SETTING_ID } from "./nativeContributions";
import type { SettingsRegistry } from "./registry";
import {
  resolveSettingsReturnLabel,
  resolveSettingsReturnPath,
} from "./SettingsNavigation";
import { SettingsSidebar } from "./SettingsSidebar";
import { useSettingPreview } from "./useSettingPreview";
import { useSettingsCatalog } from "./SettingsRegistryProvider";
import {
  type SettingsValuesState,
  useSettingsValues,
} from "./useSettingsValues";
import "./styles.css";

const LOCAL_TIME_PATTERN = /^(?:[01]\d|2[0-3]):[0-5]\d$/;

const scopeLabel = (scope: string): string => {
  switch (scope) {
    case "profile":
      return "Profile";
    case "device":
      return "This device";
    case "workspace":
      return "Workspace";
    case "view":
      return "This view";
    case "widget-instance":
      return "This widget";
    default:
      return scope;
  }
};

function settingElementId(settingId: SettingId): string {
  return `setting-${settingId.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}

export function settingsFocusScrollBehavior(
  prefersReducedMotion =
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches,
): ScrollBehavior {
  return prefersReducedMotion ? "auto" : "smooth";
}

function effectiveSourceLabel(
  definition: SettingDefinition,
  value?: EffectiveSettingValue,
): string {
  if (!value || value.source === "default") {
    return `Using ${definition.ownerLabel} default`;
  }
  if (value.source === "policy") return "Managed by policy";
  return `Modified for ${scopeLabel(value.source).toLocaleLowerCase()}`;
}

function SettingMeta({
  definition,
  value,
}: {
  readonly definition: SettingDefinition;
  readonly value?: EffectiveSettingValue;
}) {
  return (
    <div className="wb-settings-meta" aria-label="Setting details">
      <span className="wb-settings-badge">
        {scopeLabel(definition.defaultScope)}
      </span>
      <span>{effectiveSourceLabel(definition, value)}</span>
      <span>{definition.provenance.label}</span>
      <code>{definition.settingId}</code>
    </div>
  );
}

function SettingCard({
  projected,
  value,
  children,
}: {
  readonly projected: ProjectedSetting;
  readonly value?: EffectiveSettingValue;
  readonly children: ReactNode;
}) {
  const { definition, placement } = projected;
  return (
    <article
      id={settingElementId(definition.settingId)}
      className="wb-settings-card"
      tabIndex={-1}
      data-setting-id={definition.settingId}
    >
      <div className="wb-settings-card__heading">
        <div>
          <h3>{definition.title}</h3>
          <p>{placement.contextualSummary ?? definition.summary}</p>
        </div>
        <span className="wb-settings-card__value">
          {scopeLabel(definition.defaultScope)}
        </span>
      </div>
      {definition.details ? (
        <p className="wb-settings-card__details">{definition.details}</p>
      ) : null}
      <div className="wb-settings-card__control">{children}</div>
      <footer className="wb-settings-card__footer">
        <SettingMeta definition={definition} value={value} />
      </footer>
    </article>
  );
}

function TypographyScaleSetting({ projected }: { readonly projected: ProjectedSetting }) {
  const { scale, option, setScale, resetScale } = useTypographyScale();
  const selectedIndex = TYPOGRAPHY_SCALE_OPTIONS.findIndex(
    (candidate) => candidate.value === scale,
  );
  const definition = projected.definition;
  const value: EffectiveSettingValue = {
    settingId: definition.settingId,
    scope: { kind: "device", subjectId: "current" },
    effectiveValue: scale,
    configuredValue: scale === DEFAULT_TYPOGRAPHY_SCALE ? undefined : scale,
    source: scale === DEFAULT_TYPOGRAPHY_SCALE ? "default" : "device",
    isModified: scale !== DEFAULT_TYPOGRAPHY_SCALE,
    revision: `local:${scale}`,
    diagnostics: [],
  };

  return (
    <SettingCard projected={projected} value={value}>
      <div className="wb-type-scale-control">
        <div className="wb-type-scale-control__label-row">
          <label htmlFor="wb-type-scale">Text size</label>
          <output aria-live="polite">
            {option.label} · {option.percentage}%
          </output>
        </div>
        <input
          id="wb-type-scale"
          type="range"
          min={0}
          max={TYPOGRAPHY_SCALE_OPTIONS.length - 1}
          step={1}
          value={selectedIndex}
          style={
            {
              "--wb-type-scale-progress": `${
                (selectedIndex / (TYPOGRAPHY_SCALE_OPTIONS.length - 1)) * 100
              }%`,
            } as CSSProperties
          }
          aria-valuetext={`${option.label}, ${option.percentage}%`}
          onChange={(event) => {
            const next = TYPOGRAPHY_SCALE_OPTIONS[Number(event.target.value)];
            if (next) setScale(next.value);
          }}
        />
        <div className="wb-type-scale-control__ticks" aria-hidden="true">
          {TYPOGRAPHY_SCALE_OPTIONS.map((candidate) => (
            <span key={candidate.value}>{candidate.label}</span>
          ))}
        </div>
      </div>

      <div className="wb-type-scale-preview" aria-label="Text size preview">
        <span className="wb-type-scale-preview__label">Preview</span>
        <strong>A clear dashboard keeps your work within reach.</strong>
        <p>
          Supporting details remain readable without allowing individual
          components to invent smaller fine print.
        </p>
      </div>

      <div className="wb-settings-control-actions">
        <p>{option.description}</p>
        <Button
          variant="secondary"
          size="small"
          disabled={scale === DEFAULT_TYPOGRAPHY_SCALE}
          onClick={resetScale}
        >
          Reset to standard
        </Button>
      </div>
    </SettingCard>
  );
}

function formatLocalTime(value: string): string {
  if (!LOCAL_TIME_PATTERN.test(value)) return value;
  const [hourText, minuteText] = value.split(":");
  const instant = new Date(2000, 0, 1, Number(hourText), Number(minuteText));
  return new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
  }).format(instant);
}

export function formatInstantInTimeZone(
  value: string,
  timezone: string | undefined,
): string {
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return value;
  try {
    return new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      timeZoneName: "short",
      ...(timezone ? { timeZone: timezone } : {}),
    }).format(instant);
  } catch {
    return instant.toISOString();
  }
}

function describeImpactPreview(
  value: unknown,
  timezone: string | undefined,
): string | undefined {
  if (typeof value === "string") return value;
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return undefined;
  }
  const preview = value as Record<string, unknown>;
  const pendingDay = preview.first_effective_window ?? preview.pending_day;
  if (
    typeof pendingDay === "object" &&
    pendingDay !== null &&
    !Array.isArray(pendingDay)
  ) {
    const record = pendingDay as Record<string, unknown>;
    const start = record.window_start;
    const end = record.window_end;
    if (typeof start === "string" && typeof end === "string") {
      return `The first Journal window using this boundary will run from ${formatInstantInTimeZone(start, timezone)} to ${formatInstantInTimeZone(end, timezone)}.`;
    }
  }
  return undefined;
}

function describeBridgeInterval(
  value: unknown,
  timezone: string | undefined,
): string | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return undefined;
  }
  const preview = value as Record<string, unknown>;
  const current = preview.current_day;
  const proposed = preview.first_effective_window ?? preview.pending_day;
  if (
    typeof current !== "object" ||
    current === null ||
    Array.isArray(current) ||
    typeof proposed !== "object" ||
    proposed === null ||
    Array.isArray(proposed)
  ) {
    return undefined;
  }
  const currentEnd = (current as Record<string, unknown>).window_end;
  const proposedStart = (proposed as Record<string, unknown>).window_start;
  if (typeof currentEnd !== "string" || typeof proposedStart !== "string") {
    return undefined;
  }
  const currentEndMs = Date.parse(currentEnd);
  const proposedStartMs = Date.parse(proposedStart);
  if (Number.isNaN(currentEndMs) || Number.isNaN(proposedStartMs)) return undefined;
  if (currentEndMs === proposedStartMs) {
    return "The transition is contiguous; it introduces no gap or overlap.";
  }
  const relation = proposedStartMs > currentEndMs ? "bridge gap" : "overlap";
  const first = proposedStartMs > currentEndMs ? currentEnd : proposedStart;
  const second = proposedStartMs > currentEndMs ? proposedStart : currentEnd;
  return `Transition ${relation}: ${formatInstantInTimeZone(first, timezone)} to ${formatInstantInTimeZone(second, timezone)}.`;
}

function TimeSetting({
  projected,
  values,
}: {
  readonly projected: ProjectedSetting;
  readonly values: SettingsValuesState;
}) {
  const { definition } = projected;
  const value = values.snapshot?.values.get(definition.settingId);
  const storedValue =
    value?.pendingValue ??
    value?.configuredValue ??
    value?.effectiveValue ??
    definition.defaultValue;
  const initialValue = typeof storedValue === "string" ? storedValue : "05:00";
  const [draft, setDraft] = useState(initialValue);
  const revision = value?.revision ?? "unavailable";
  const [baseline, setBaseline] = useState({ revision, value: initialValue });
  const [externalChange, setExternalChange] = useState(false);

  useEffect(() => {
    if (revision === baseline.revision) return;
    const mayAdopt = draft === baseline.value || draft === initialValue;
    if (mayAdopt) setDraft(initialValue);
    setExternalChange(!mayAdopt);
    setBaseline({ revision, value: initialValue });
  }, [baseline, draft, initialValue, revision]);

  const serverUnavailable = values.status === "unavailable" || values.status === "error";
  const disabled =
    serverUnavailable ||
    values.status === "loading" ||
    values.snapshot?.readOnly === true ||
    values.mutationSettingId === definition.settingId;
  const changed = draft !== initialValue;
  const valid = LOCAL_TIME_PATTERN.test(draft);
  const impactPreview = describeImpactPreview(
    value?.impactPreview,
    values.snapshot?.timezone,
  );
  const proposal = useSettingPreview({
    settingId: definition.settingId,
    value: draft,
    expectedRevision: value?.revision,
    enabled: changed && valid && !disabled,
  });
  const proposalMatches =
    proposal.status === "ready" &&
    proposal.preview?.value === draft &&
    proposal.preview.valueRevision === value?.revision;
  const proposedImpact = describeImpactPreview(
    proposal.preview?.impactPreview,
    proposal.preview?.timezone ?? values.snapshot?.timezone,
  );
  const bridgeImpact = describeBridgeInterval(
    proposal.preview?.impactPreview,
    proposal.preview?.timezone ?? values.snapshot?.timezone,
  );

  return (
    <SettingCard projected={projected} value={value}>
      {values.status === "loading" ? (
        <InlineAlert tone="info">Loading the authoritative Journal setting…</InlineAlert>
      ) : null}
      {serverUnavailable ? (
        <InlineAlert tone="warning">
          The Settings service is unavailable. This setting remains discoverable,
          but editing is disabled until its Journal authority reconnects.
        </InlineAlert>
      ) : null}
      {values.snapshot?.readOnly ? (
        <InlineAlert tone="warning">
          Work Buddy is read-only. You can inspect this setting but cannot change it.
        </InlineAlert>
      ) : null}
      {externalChange ? (
        <InlineAlert tone="warning" role="status" aria-live="polite">
          <span>
            The authoritative value changed to {formatLocalTime(initialValue)} while
            you were editing. Your draft was preserved.
          </span>
          <Button
            variant="secondary"
            size="small"
            onClick={() => {
              setDraft(initialValue);
              setExternalChange(false);
            }}
          >
            Use latest value
          </Button>
        </InlineAlert>
      ) : null}

      <div className="wb-time-setting-control">
        <label htmlFor={`control-${settingElementId(definition.settingId)}`}>
          Day starts
        </label>
        <div className="wb-time-setting-control__row">
          <input
            id={`control-${settingElementId(definition.settingId)}`}
            type="time"
            step={(definition.control.kind === "time"
              ? definition.control.minuteStep ?? 15
              : 15) * 60}
            value={draft}
            disabled={disabled}
            aria-invalid={!valid}
            onChange={(event) => {
              setDraft(event.target.value);
              if (event.target.value === initialValue) setExternalChange(false);
            }}
          />
          <Button
            variant="primary"
            size="small"
            disabled={disabled || !changed || !valid || !proposalMatches}
            onClick={() => void values.write(definition.settingId, draft)}
          >
            Save change
          </Button>
        </div>
        {proposal.status === "waiting" || proposal.status === "loading" ? (
          <p role="status" aria-live="polite">
            Previewing this change against the authoritative Journal policy…
          </p>
        ) : null}
        {proposal.status === "error" ? (
          <InlineAlert tone="danger" role="alert" aria-live="assertive">
            This change cannot be saved until its impact can be previewed. {proposal.error}
          </InlineAlert>
        ) : null}
        {proposalMatches && proposal.preview ? (
          <section className="wb-setting-proposal" aria-label="Unsaved change preview">
            <header>
              <strong>Preview · not saved</strong>
              <span>{formatLocalTime(String(proposal.preview.value))}</span>
            </header>
            {proposal.preview.effectiveAt ? (
              <p>
                If saved, this becomes effective {formatInstantInTimeZone(
                  proposal.preview.effectiveAt,
                  proposal.preview.timezone ?? values.snapshot?.timezone,
                )}.
              </p>
            ) : null}
            {proposedImpact ? <p>{proposedImpact}</p> : null}
            {bridgeImpact ? <p>{bridgeImpact}</p> : null}
            {proposal.preview.diagnostics.map((diagnostic) => (
              <p key={`${diagnostic.code}:${diagnostic.message}`}>
                <strong>{diagnostic.code}</strong>: {diagnostic.message}
              </p>
            ))}
          </section>
        ) : null}
        <p>
          With {formatLocalTime(draft)} selected, work after midnight but before
          this time remains in the previous Journal day.
        </p>
        {values.snapshot?.timezone ? (
          <p className="wb-time-setting-control__timezone">
            Interpreted in Work Buddy timezone: <strong>{values.snapshot.timezone}</strong>
          </p>
        ) : null}
        {impactPreview ? (
          <p className="wb-time-setting-control__impact">{impactPreview}</p>
        ) : null}
        {value?.pendingValue !== undefined ? (
          <InlineAlert tone="info">
            {formatLocalTime(String(value.pendingValue))} is saved and will become
            effective{value.effectiveAt
              ? ` ${formatInstantInTimeZone(value.effectiveAt, values.snapshot?.timezone)}`
              : " at the next boundary"}.
            Existing Journal days keep the boundary they were created with.
          </InlineAlert>
        ) : null}
      </div>

      <div className="wb-settings-control-actions">
        <p>
          Changes begin with the next safe Journal boundary; historical day identity
          is never silently reinterpreted.
        </p>
        <Button
          variant="secondary"
          size="small"
          disabled={disabled || !value?.isModified}
          onClick={() => void values.reset(definition.settingId)}
        >
          Reset to Journal default
        </Button>
      </div>
    </SettingCard>
  );
}

function UnsupportedSetting({ projected }: { readonly projected: ProjectedSetting }) {
  return (
    <SettingCard projected={projected}>
      <InlineAlert tone="warning">
        This installed setting uses a control that this dashboard version cannot yet
        render. Its definition and personal value have been preserved.
      </InlineAlert>
    </SettingCard>
  );
}

function SettingControl({
  projected,
  values,
}: {
  readonly projected: ProjectedSetting;
  readonly values: SettingsValuesState;
}) {
  if (projected.definition.control.kind === "typography-scale") {
    return <TypographyScaleSetting projected={projected} />;
  }
  if (
    projected.definition.control.kind === "time" &&
    projected.definition.settingId === JOURNAL_DAY_BOUNDARY_SETTING_ID
  ) {
    return <TimeSetting projected={projected} values={values} />;
  }
  return <UnsupportedSetting projected={projected} />;
}

function PageProjection({
  registry,
  page,
}: {
  readonly registry: SettingsRegistry;
  readonly page: SettingsPageContribution;
}) {
  const location = useLocation();
  const projection = registry.projectPage(page.pageId);
  const [pageSearchQuery, setPageSearchQuery] = useState("");
  const visibleProjection = registry.searchPage(page.pageId, pageSearchQuery);
  const requiresServerValues =
    projection?.sections.some((section) =>
      section.settings.some(
        (setting) => setting.definition.control.kind !== "typography-scale",
      ),
    ) ?? false;
  const values = useSettingsValues(page.pageId, requiresServerValues);
  const focusSettingId = useMemo(() => {
    const params = new URLSearchParams(location.search);
    return params.get("setting") ?? params.get("focus");
  }, [location.search]);

  useEffect(() => {
    if (!focusSettingId) return;
    const target = document.getElementById(
      settingElementId(focusSettingId as SettingId),
    );
    target?.focus({ preventScroll: true });
    target?.scrollIntoView?.({
      behavior: settingsFocusScrollBehavior(),
      block: "center",
    });
  }, [focusSettingId, projection]);

  useEffect(() => {
    setPageSearchQuery("");
  }, [page.pageId]);

  if (!projection) return null;
  if (page.navigationGroup === "status") {
    return (
      <section className="wb-settings-content wb-settings-content--status">
        <ControlStatusPage />
      </section>
    );
  }
  const visibleSettingCount =
    visibleProjection?.sections.reduce(
      (total, section) => total + section.settings.length,
      0,
    ) ?? 0;
  const visibleSectionIds = new Set(
    visibleProjection?.sections.map((section) => section.definition.sectionId),
  );
  const visiblePlacementIds = new Set(
    visibleProjection?.sections.flatMap((section) =>
      section.settings.map((setting) => setting.placement.placementId),
    ),
  );
  return (
    <section className="wb-settings-content" aria-labelledby="settings-title">
      <header className="wb-settings-content__header">
        <p className="wb-settings-content__eyebrow">{page.context.label}</p>
        <h1 id="settings-title">{page.label}</h1>
        <p>{page.description}</p>
      </header>

      <label className="wb-settings-page-search">
        <span className="wb-visually-hidden">
          Search within {page.navigationLabel} settings
        </span>
        <MagnifyingGlass weight="bold" aria-hidden="true" />
        <input
          type="search"
          value={pageSearchQuery}
          placeholder={`Search ${page.navigationLabel} settings…`}
          onChange={(event) => setPageSearchQuery(event.target.value)}
        />
        {pageSearchQuery.trim() ? (
          <>
            <span
              className="wb-settings-page-search__count"
              role="status"
              aria-live="polite"
            >
              {visibleSettingCount === 1
                ? "1 setting"
                : `${visibleSettingCount} settings`}
            </span>
            <IconButton
              label={`Clear ${page.navigationLabel} settings search`}
              icon={<X weight="bold" />}
              variant="ghost"
              size="small"
              className="wb-settings-page-search__clear"
              onClick={() => setPageSearchQuery("")}
            />
          </>
        ) : null}
      </label>

      {values.message ? (
        <InlineAlert tone="success" role="status" aria-live="polite">
          {values.message}
        </InlineAlert>
      ) : null}
      {values.error ? (
        <InlineAlert tone="danger" role="alert" aria-live="assertive">
          {values.error}
        </InlineAlert>
      ) : null}
      {values.snapshot?.diagnostics.map((diagnostic) => (
        <InlineAlert
          key={`${diagnostic.code}:${diagnostic.message}`}
          tone="warning"
          role="status"
          aria-live="polite"
        >
          <span>{diagnostic.message}</span>{" "}
          <code>{diagnostic.code}</code>
        </InlineAlert>
      ))}

      {projection.sections.map((section) => (
        <section
          key={section.definition.sectionId}
          className="wb-settings-section"
          aria-labelledby={`section-${section.definition.sectionId}`}
          hidden={!visibleSectionIds.has(section.definition.sectionId)}
        >
          <header className="wb-settings-section__header">
            <h2 id={`section-${section.definition.sectionId}`}>
              {section.definition.label}
            </h2>
            {section.definition.description ? (
              <p>{section.definition.description}</p>
            ) : null}
          </header>
          <div className="wb-settings-section__items">
            {section.settings.map((projected) => (
              <div
                key={projected.placement.placementId}
                hidden={!visiblePlacementIds.has(projected.placement.placementId)}
              >
                <SettingControl projected={projected} values={values} />
              </div>
            ))}
          </div>
        </section>
      ))}
      {pageSearchQuery.trim() && visibleSettingCount === 0 ? (
        <p className="wb-settings-page-search-empty">
          No settings on this page match “{pageSearchQuery.trim()}”.
        </p>
      ) : null}
    </section>
  );
}

export function SettingsPage({
  defaultViewPath = "/journal",
  registryOverride,
}: {
  readonly defaultViewPath?: string;
  readonly registryOverride?: SettingsRegistry;
}) {
  const remote = useSettingsCatalog();
  const registry = registryOverride ?? remote.registry;
  const location = useLocation();
  const navigate = useNavigate();
  const [searchQuery, setSearchQuery] = useState("");
  const canonicalSettingId = useMemo(() => {
    const match = location.pathname.match(/^\/settings\/setting\/(.+)$/);
    if (!match) return undefined;
    try {
      return decodeURIComponent(match[1]);
    } catch {
      return undefined;
    }
  }, [location.pathname]);
  const canonicalResult = canonicalSettingId
    ? registry
        .search(canonicalSettingId)
        .find((result) => result.settingId === canonicalSettingId)
    : undefined;
  const page = registry.getPageByRoute(location.pathname);
  const returnPath = resolveSettingsReturnPath(
    location.state,
    page?.fallbackReturnPath ?? defaultViewPath,
  );
  const returnLabel = resolveSettingsReturnLabel(location.state);

  useEffect(() => {
    if (!canonicalResult) return;
    navigate(
      `${canonicalResult.page.route}?setting=${encodeURIComponent(canonicalResult.settingId)}`,
      { replace: true, state: location.state },
    );
  }, [canonicalResult, location.state, navigate]);

  return (
    <main className="wb-settings-shell">
      <SettingsSidebar
        registry={registry}
        searchQuery={searchQuery}
        returnLabel={returnLabel}
        onSearchQueryChange={setSearchQuery}
        onReturn={() => navigate(returnPath)}
      />
      {page ? (
        <PageProjection registry={registry} page={page} />
      ) : canonicalResult ? (
        <section className="wb-settings-content" aria-live="polite">
          <p>Opening {canonicalResult.definition.title}…</p>
        </section>
      ) : (
        <section className="wb-settings-content" aria-labelledby="settings-title">
          <header className="wb-settings-content__header">
            <p className="wb-settings-content__eyebrow">Settings</p>
            <h1 id="settings-title">
              {canonicalSettingId ? "Setting not found" : "Settings page not found"}
            </h1>
            <p>
              {canonicalSettingId
                ? `No installed contribution currently defines ${canonicalSettingId}. Its stored value, if any, has not been deleted.`
                : "This settings contribution is not installed or its route is no longer available. Your stored values have not been deleted."}
            </p>
          </header>
          <Button onClick={() => navigate("/settings/system/accessibility")}>
            Open Accessibility
          </Button>
        </section>
      )}
    </main>
  );
}
