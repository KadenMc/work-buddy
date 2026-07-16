import {
  asSettingsPageId,
  type SettingsPageId,
} from "../dashboard/contributions/contracts";
import {
  asSettingId,
  asSettingPlacementId,
  type EffectiveSettingSource,
  type EffectiveSettingValue,
  type SettingsAppCategory,
  type SettingApplyBehavior,
  type SettingDefinition,
  type SettingValueScope,
  type SettingsContextKind,
  type SettingsContribution,
  type SettingsNavigationGroup,
  type SettingsPageContribution,
  type ProposedSettingPreview,
  type SettingsDiagnostic,
  type SettingsValueSnapshot,
  type StandardSettingControl,
} from "./contracts";

type JsonRecord = Record<string, unknown>;

const isRecord = (value: unknown): value is JsonRecord =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const array = (value: unknown): readonly unknown[] =>
  Array.isArray(value) ? value : [];

function requiredString(record: JsonRecord, ...names: string[]): string {
  for (const name of names) {
    const value = record[name];
    if (typeof value === "string" && value.length > 0) return value;
  }
  throw new Error(`Missing settings field: ${names[0]}`);
}

function optionalString(record: JsonRecord, ...names: string[]): string | undefined {
  for (const name of names) {
    const value = record[name];
    if (typeof value === "string" && value.length > 0) return value;
  }
  return undefined;
}

function normalizeSettingsDiagnostic(value: unknown): SettingsDiagnostic | undefined {
  if (typeof value === "string" && value.length > 0) {
    return { code: "diagnostic", message: value };
  }
  if (!isRecord(value)) return undefined;

  const code = optionalString(value, "code") ?? "diagnostic";
  const message = optionalString(value, "message") ?? code;
  return {
    code,
    message,
    activeTimezone: optionalString(value, "active_timezone", "activeTimezone"),
    configuredTimezone: optionalString(
      value,
      "configured_timezone",
      "configuredTimezone",
    ),
  };
}

const numeric = (value: unknown, fallback: number): number =>
  typeof value === "number" && Number.isFinite(value) ? value : fallback;

function normalizeRoute(route: string): string {
  return route.startsWith("/app/settings/") ? route.slice(4) : route;
}

function normalizeContext(value: unknown): {
  kind: SettingsContextKind;
  id: string;
  label: string;
} {
  if (!isRecord(value)) throw new Error("Settings context must be an object");
  const rawKind = requiredString(value, "kind");
  const kind = rawKind === "work-buddy" || rawKind === "core-area"
    ? "system"
    : rawKind === "component"
      ? "connection"
      : rawKind;
  if (
    ![
      "system",
      "app",
      "subsystem",
      "view",
      "connection",
      "component",
      "widget-type",
      "widget-instance",
      "status",
    ].includes(kind)
  ) {
    throw new Error(`Unsupported settings context kind: ${rawKind}`);
  }
  return {
    kind: kind as SettingsContextKind,
    id: requiredString(value, "id"),
    label: optionalString(value, "label") ?? requiredString(value, "id"),
  };
}

function normalizeGroup(value: unknown): SettingsNavigationGroup {
  const group = value === "work-buddy" ? "system" : value;
  if (
    group !== "system" &&
    group !== "apps" &&
    group !== "connections" &&
    group !== "status"
  ) {
    throw new Error(`Unsupported settings navigation group: ${String(value)}`);
  }
  return group;
}

function normalizeAppCategory(value: unknown): SettingsAppCategory {
  if (value === "built-in" || value === "personal" || value === "community") {
    return value;
  }
  throw new Error(`Unsupported or missing App settings category: ${String(value)}`);
}

function normalizeScope(value: unknown): SettingValueScope {
  if (
    value !== "profile" &&
    value !== "workspace" &&
    value !== "device" &&
    value !== "view" &&
    value !== "widget-instance"
  ) {
    throw new Error(`Unsupported setting scope: ${String(value)}`);
  }
  return value;
}

function normalizeApplyBehavior(value: unknown): SettingApplyBehavior {
  switch (value) {
    case "live":
      return "immediate";
    case "reload-view":
    case "restart-component":
    case "restart-dashboard":
    case "next-boundary":
    case "immediate":
      return value;
    default:
      return "immediate";
  }
}

function normalizeTrustTier(value: unknown) {
  if (
    value === "native" ||
    value === "curated" ||
    value === "community" ||
    value === "developer-local"
  ) {
    return value;
  }
  throw new Error(`Unsupported or missing settings trust tier: ${String(value)}`);
}

function normalizeControl(value: unknown): StandardSettingControl {
  if (typeof value === "string") {
    if (value === "time") return { kind: "time" };
    if (value === "switch") return { kind: "switch" };
  }
  if (!isRecord(value)) throw new Error("Unsupported settings control");
  const kind = requiredString(value, "kind", "control");
  if (kind === "time") {
    return { kind: "time", minuteStep: numeric(value.minute_step ?? value.minuteStep, 15) };
  }
  if (kind === "switch") return { kind: "switch" };
  if (kind === "typography-scale") {
    return {
      kind,
      options: array(value.options).filter(
        (option): option is string => typeof option === "string",
      ),
    };
  }
  if (kind === "select") {
    return {
      kind,
      options: array(value.options).map((option) => {
        if (!isRecord(option)) throw new Error("Invalid select option");
        return {
          value: requiredString(option, "value"),
          label: requiredString(option, "label"),
          description: optionalString(option, "description"),
        };
      }),
    };
  }
  throw new Error(`Unsupported settings control: ${kind}`);
}

function normalizeDefinition(value: unknown): SettingDefinition {
  if (!isRecord(value)) throw new Error("Setting definition must be an object");
  const owner = isRecord(value.owner) ? value.owner : undefined;
  const provenance = isRecord(value.provenance) ? value.provenance : undefined;
  const presentation = isRecord(value.presentation) ? value.presentation : undefined;
  const ownerId =
    optionalString(value, "owner_id", "ownerId") ??
    (owner ? requiredString(owner, "id") : requiredString(value, "owner_app_id"));
  const presentationControl = presentation && typeof presentation.control === "string"
    ? { ...presentation, kind: presentation.control }
    : presentation;
  const control = normalizeControl(value.control ?? presentationControl);
  const defaultScope = normalizeScope(
    value.default_scope ?? value.defaultScope ?? array(value.allowed_scopes)[0] ?? "profile",
  );
  const sensitivity = optionalString(value, "sensitivity") ??
    (value.visibility === "secret" ? "secret-reference" : "ordinary");
  if (
    sensitivity !== "ordinary" &&
    sensitivity !== "private" &&
    sensitivity !== "secret-reference"
  ) {
    throw new Error(`Unsupported setting sensitivity: ${sensitivity}`);
  }
  return {
    schemaVersion: 1,
    settingId: asSettingId(requiredString(value, "setting_id", "settingId")),
    definitionVersion: numeric(
      value.definition_version ?? value.definitionVersion,
      1,
    ),
    valueVersion: numeric(value.value_version ?? value.valueVersion, 1),
    ownerId,
    ownerLabel:
      optionalString(value, "owner_label", "ownerLabel") ??
      optionalString(owner ?? {}, "label") ??
      ownerId,
    provenance: {
      complementId:
        optionalString(provenance ?? {}, "complement_id", "complementId") ?? ownerId,
      complementVersion:
        optionalString(provenance ?? {}, "complement_version", "complementVersion") ??
        "server",
      trustTier: normalizeTrustTier(
        provenance?.trust_tier ?? provenance?.trustTier,
      ),
      label:
        optionalString(provenance ?? {}, "label") ?? `Provided by ${ownerId}`,
    },
    title: requiredString(value, "title"),
    summary: requiredString(value, "summary", "short_description", "shortDescription"),
    details: optionalString(value, "details", "long_description", "longDescription"),
    valueSchema: value.value_schema ?? value.valueSchema,
    defaultValue: value.default_value ?? value.defaultValue,
    allowedScopes: array(value.allowed_scopes ?? value.allowedScopes).map(normalizeScope),
    defaultScope,
    control,
    appliesTo: array(value.applies_to ?? value.appliesTo).map(normalizeContext),
    applyBehavior: normalizeApplyBehavior(
      value.apply_behavior ?? value.applyBehavior ?? presentation?.apply_behavior ??
        presentation?.applyBehavior,
    ),
    sensitivity,
    visibility:
      value.visibility === "frontend" ||
      value.visibility === "backend" ||
      value.visibility === "secret"
        ? value.visibility
        : "backend",
    searchKeywords: array(
      value.search_keywords ?? value.searchKeywords ?? value.keywords,
    ).filter(
      (candidate): candidate is string => typeof candidate === "string",
    ),
  };
}

function normalizePage(value: unknown): SettingsPageContribution {
  if (!isRecord(value)) throw new Error("Settings page must be an object");
  const context = normalizeContext(value.context);
  const route = normalizeRoute(requiredString(value, "route"));
  const owner = isRecord(value.owner) ? value.owner : undefined;
  const rawLabel = requiredString(value, "label");
  const navigationGroup = normalizeGroup(
    value.navigation_group ?? value.navigationGroup ?? context.kind,
  );
  const label = rawLabel.toLocaleLowerCase().includes("settings")
    ? rawLabel
    : navigationGroup === "apps"
      ? `${rawLabel} settings`
      : rawLabel;
  return {
    schemaVersion: 1,
    pageId: asSettingsPageId(requiredString(value, "page_id", "pageId")),
    ownerId:
      optionalString(value, "owner_id", "ownerId", "owner_app_id") ??
      (owner ? requiredString(owner, "id") : "wb.core"),
    route,
    label,
    description: optionalString(value, "description") ?? "",
    navigationGroup,
    navigationLabel:
      optionalString(value, "navigation_label", "navigationLabel") ??
      rawLabel,
    navigationOrder: numeric(
      value.navigation_order ?? value.navigationOrder ?? value.order,
      100,
    ),
    appCategory:
      navigationGroup === "apps"
        ? normalizeAppCategory(
            value.navigation_category ?? value.navigationCategory,
          )
        : undefined,
    context,
    sections: array(value.sections).map((sectionValue, index) => {
      if (!isRecord(sectionValue)) throw new Error("Settings section must be an object");
      return {
        sectionId: requiredString(sectionValue, "section_id", "sectionId"),
        label: requiredString(sectionValue, "label"),
        description: optionalString(sectionValue, "description"),
        order: numeric(sectionValue.order, (index + 1) * 10),
      };
    }),
    fallbackReturnPath: optionalString(
      value,
      "fallback_return_path",
      "fallbackReturnPath",
    ),
  };
}

export interface ServerRegistryResult {
  readonly registryRevision: string;
  readonly contribution: SettingsContribution;
}

export function normalizeServerRegistry(payload: unknown): ServerRegistryResult {
  if (!isRecord(payload) || payload.schema_version !== 1) {
    throw new Error("Unsupported settings registry payload");
  }
  const pages = array(payload.pages).map(normalizePage);
  return {
    registryRevision: requiredString(payload, "registry_revision"),
    contribution: {
      sourceId: `wb.server.${requiredString(payload, "registry_revision")}`,
      definitions: array(payload.definitions).map(normalizeDefinition),
      pages,
      placements: array(payload.placements).map((placementValue) => {
        if (!isRecord(placementValue)) {
          throw new Error("Setting placement must be an object");
        }
        return {
          schemaVersion: 1,
          placementId: asSettingPlacementId(
            requiredString(placementValue, "placement_id", "placementId"),
          ),
          settingId: asSettingId(
            requiredString(placementValue, "setting_id", "settingId"),
          ),
          pageId: asSettingsPageId(
            requiredString(placementValue, "page_id", "pageId"),
          ),
          sectionId: requiredString(placementValue, "section_id", "sectionId"),
          order: numeric(placementValue.order, 100),
          contextualSummary: optionalString(
            placementValue,
            "contextual_summary",
            "contextualSummary",
            "contextual_note",
          ),
          preferredForSearch:
            placementValue.preferred_for_search === true ||
            placementValue.preferredForSearch === true,
        };
      }),
    },
  };
}

function normalizeSource(value: unknown): EffectiveSettingSource {
  return value === "profile" ||
    value === "workspace" ||
    value === "device" ||
    value === "view" ||
    value === "policy"
    ? value
    : "default";
}

export function normalizeEffectiveValue(value: unknown): EffectiveSettingValue {
  if (!isRecord(value)) throw new Error("Setting value must be an object");
  const scope = isRecord(value.scope) ? value.scope : {};
  return {
    settingId: asSettingId(requiredString(value, "setting_id", "settingId")),
    scope: {
      kind: normalizeScope(scope.kind ?? value.scope ?? "profile"),
      subjectId: optionalString(scope, "subject_id", "subjectId"),
    },
    effectiveValue: value.effective_value ?? value.effectiveValue,
    configuredValue: value.configured_value ?? value.configuredValue,
    source: normalizeSource(value.source),
    isModified: value.is_modified === true || value.isModified === true,
    revision: requiredString(value, "revision"),
    pendingValue: value.pending_value ?? value.pendingValue,
    effectiveAt: optionalString(value, "effective_at", "effectiveAt"),
    applyStatus: optionalString(value, "apply_status", "applyStatus"),
    impactPreview: value.impact_preview ?? value.impactPreview,
    policyTimezone: optionalString(value, "policy_timezone", "policyTimezone"),
    configuredTimezone: optionalString(
      value,
      "configured_timezone",
      "configuredTimezone",
    ),
    pendingTimezone: optionalString(value, "pending_timezone", "pendingTimezone"),
    diagnostics: array(value.diagnostics)
      .map(normalizeSettingsDiagnostic)
      .filter((diagnostic): diagnostic is SettingsDiagnostic => diagnostic !== undefined),
  };
}

export function normalizeValueSnapshot(payload: unknown): SettingsValueSnapshot {
  if (!isRecord(payload) || payload.schema_version !== 1) {
    throw new Error("Unsupported settings values payload");
  }
  const values = array(payload.values).map(normalizeEffectiveValue);
  return {
    registryRevision: requiredString(payload, "registry_revision"),
    timezone: optionalString(payload, "timezone"),
    configuredTimezone: optionalString(payload, "configured_timezone"),
    observedAt: requiredString(payload, "observed_at"),
    readOnly: payload.read_only === true,
    diagnostics: array(payload.diagnostics)
      .map(normalizeSettingsDiagnostic)
      .filter((diagnostic): diagnostic is SettingsDiagnostic => diagnostic !== undefined),
    values: new Map(values.map((value) => [value.settingId, value])),
  };
}

export class SettingsServerError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly authoritativeValue?: EffectiveSettingValue,
  ) {
    super(message);
    this.name = "SettingsServerError";
  }
}

async function responseJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

async function requireSuccess(response: Response): Promise<unknown> {
  const payload = await responseJson(response);
  if (response.ok) return payload;
  if (isRecord(payload)) {
    throw new SettingsServerError(
      optionalString(payload, "error") ?? `http_${response.status}`,
      optionalString(payload, "message") ?? `Settings request failed (${response.status})`,
      payload.value === undefined ? undefined : normalizeEffectiveValue(payload.value),
    );
  }
  throw new SettingsServerError(
    `http_${response.status}`,
    `Settings request failed (${response.status})`,
  );
}

export async function fetchSettingsRegistry(
  fetchImpl: typeof fetch = fetch,
  signal?: AbortSignal,
): Promise<ServerRegistryResult | undefined> {
  const response = await fetchImpl("/api/settings/registry", {
    headers: { Accept: "application/json" },
    signal,
  });
  if (response.status === 404) return undefined;
  return normalizeServerRegistry(await requireSuccess(response));
}

export async function fetchSettingsValues(
  contextId: string,
  fetchImpl: typeof fetch = fetch,
  signal?: AbortSignal,
): Promise<SettingsValueSnapshot | undefined> {
  const query = new URLSearchParams({ context_id: contextId });
  const response = await fetchImpl(`/api/settings/values?${query}`, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (response.status === 404) return undefined;
  return normalizeValueSnapshot(await requireSuccess(response));
}

interface ValueMutationResponse {
  readonly value: EffectiveSettingValue;
  readonly timezone?: string;
  readonly registryRevision: string;
}

function normalizeMutationResponse(payload: unknown): ValueMutationResponse {
  if (!isRecord(payload) || payload.schema_version !== 1) {
    throw new Error("Unsupported settings mutation payload");
  }
  return {
    value: normalizeEffectiveValue(payload.value),
    timezone: optionalString(payload, "timezone"),
    registryRevision: requiredString(payload, "registry_revision"),
  };
}

export async function patchSettingValue(
  settingId: string,
  value: unknown,
  expectedRevision: string,
  fetchImpl: typeof fetch = fetch,
): Promise<ValueMutationResponse> {
  const response = await fetchImpl(
    `/api/settings/values/${encodeURIComponent(settingId)}`,
    {
      method: "PATCH",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({
        scope: "profile",
        value,
        expected_revision: expectedRevision,
      }),
    },
  );
  return normalizeMutationResponse(await requireSuccess(response));
}

export async function deleteSettingValue(
  settingId: string,
  expectedRevision: string,
  fetchImpl: typeof fetch = fetch,
): Promise<ValueMutationResponse> {
  const response = await fetchImpl(
    `/api/settings/values/${encodeURIComponent(settingId)}`,
    {
      method: "DELETE",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({
        scope: "profile",
        expected_revision: expectedRevision,
      }),
    },
  );
  return normalizeMutationResponse(await requireSuccess(response));
}

export async function previewSettingValue(
  settingId: string,
  value: unknown,
  expectedRevision: string,
  fetchImpl: typeof fetch = fetch,
  signal?: AbortSignal,
): Promise<ProposedSettingPreview> {
  const response = await fetchImpl(
    `/api/settings/values/${encodeURIComponent(settingId)}/preview`,
    {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({
        scope: "profile",
        value,
        expected_revision: expectedRevision,
      }),
      signal,
    },
  );
  const payload = await requireSuccess(response);
  if (!isRecord(payload) || payload.schema_version !== 1 || !isRecord(payload.preview)) {
    throw new Error("Unsupported settings preview payload");
  }
  const preview = payload.preview;
  const scope = isRecord(preview.scope) ? preview.scope : {};
  return {
    settingId: asSettingId(requiredString(preview, "setting_id")),
    scope: {
      kind: normalizeScope(scope.kind ?? "profile"),
      subjectId: optionalString(scope, "subject_id", "subjectId"),
    },
    value: preview.value,
    valueRevision: requiredString(payload, "value_revision"),
    timezone: optionalString(payload, "timezone"),
    configuredTimezone: optionalString(payload, "configured_timezone"),
    effectiveAt: optionalString(preview, "effective_at"),
    applyStatus: optionalString(preview, "apply_status") ?? "preview",
    impactPreview: preview.impact_preview,
    diagnostics: array(payload.diagnostics)
      .map(normalizeSettingsDiagnostic)
      .filter((diagnostic): diagnostic is SettingsDiagnostic => diagnostic !== undefined),
  };
}

export const settingsPageContextId = (pageId: SettingsPageId): string => pageId;
