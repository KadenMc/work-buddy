import type {
  AppContribution,
  AppId,
  DefaultWidgetSlot,
  JsonSchemaReference,
  SurfaceRegionDefinition,
  ViewDefinition,
  ViewId,
  ViewLayoutKind,
  WidgetDefinition,
  WidgetIntentEffect,
  WidgetIntentPreviewPolicy,
  WidgetModule,
  WidgetModuleId,
  WidgetRoleContract,
  WidgetRoleId,
  WidgetTypeId,
} from "./contracts";
import {
  STANDARD_WIDGET_THEME_SUPPORT,
  THEME_CONTRACT_VERSION,
  type WidgetThemeDeclaration,
  type WidgetThemeSupport,
} from "./themeContract";
import type { HelpContent } from "../help/contracts";

export const DASHBOARD_GRID_COLUMNS = 24;

export interface ValidationIssue {
  readonly code: string;
  readonly path: string;
  readonly message: string;
}

export interface ContributionValidationContext {
  readonly appIds?: ReadonlySet<AppId>;
  readonly viewDefinitions?: ReadonlyMap<ViewId, ViewDefinition>;
  readonly widgetDefinitions?: ReadonlyMap<WidgetTypeId, WidgetDefinition>;
  readonly widgetRoles?: ReadonlyMap<WidgetRoleId, WidgetRoleContract>;
  readonly widgetModules?: ReadonlyMap<WidgetModuleId, WidgetModule>;
  readonly routes?: ReadonlySet<string>;
}

export class ContributionValidationError extends Error {
  readonly issues: readonly ValidationIssue[];

  constructor(issues: readonly ValidationIssue[]) {
    super(
      `Invalid dashboard contribution:\n${issues
        .map((issue) => `- ${issue.path}: ${issue.message}`)
        .join("\n")}`,
    );
    this.name = "ContributionValidationError";
    this.issues = issues;
  }
}

const NAMESPACED_ID = /^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+$/;
const ROLE_ID = /^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+@[1-9]\d*$/;
const SLOT_ID = /^[a-z][a-z0-9-]*$/;
const REGION_ID = /^[a-z][a-z0-9-]*$/;
const INSTANCE_ID = /^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$/;
const LAYOUT_KINDS = new Set<ViewLayoutKind>(["standard-grid", "single-surface"]);
const THEME_SUPPORT = new Set<WidgetThemeSupport>([
  "light",
  "dark",
  "forced-colors",
  "reduced-motion",
]);
const WIDGET_INTENT_EFFECTS = new Set<WidgetIntentEffect>([
  "read",
  "mutation",
  "navigation",
  "external",
]);
const WIDGET_INTENT_PREVIEW_POLICIES = new Set<WidgetIntentPreviewPolicy>([
  "simulate",
  "block",
]);

const addIssue = (
  issues: ValidationIssue[],
  code: string,
  path: string,
  message: string,
): void => {
  issues.push({ code, path, message });
};

const isPositiveInteger = (value: number): boolean =>
  Number.isInteger(value) && value > 0;

const validateHelpContent = (
  help: HelpContent,
  path: string,
  issues: ValidationIssue[],
): void => {
  if (help.summary.trim().length === 0) {
    addIssue(
      issues,
      "missing_help_summary",
      `${path}.summary`,
      "must provide a concise user-facing summary",
    );
  }
  if (help.details.trim().length === 0) {
    addIssue(
      issues,
      "missing_help_details",
      `${path}.details`,
      "must explain the behavior, scope, or important exceptions",
    );
  }
};

export const isNamespacedDashboardId = (value: string): boolean =>
  NAMESPACED_ID.test(value);

const validateNamespacedId = (
  value: string,
  path: string,
  issues: ValidationIssue[],
): void => {
  if (!isNamespacedDashboardId(value)) {
    addIssue(
      issues,
      "invalid_namespaced_id",
      path,
      "must be a lowercase namespaced ID such as wb.capture.quick-text",
    );
  }
};

const validateRoleId = (
  value: string,
  path: string,
  issues: ValidationIssue[],
): void => {
  if (!ROLE_ID.test(value)) {
    addIssue(
      issues,
      "invalid_role_id",
      path,
      "must be a versioned namespaced role ID such as wb.widget-role.capture@1",
    );
  }
};

const validateJsonValueInternal = (
  value: unknown,
  path: string,
  issues: ValidationIssue[],
  ancestors: Set<object>,
): void => {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return;
  }

  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      addIssue(issues, "non_json_number", path, "must be a finite JSON number");
    }
    return;
  }

  if (typeof value !== "object") {
    addIssue(issues, "non_json_value", path, "must contain only JSON-compatible values");
    return;
  }

  if (ancestors.has(value)) {
    addIssue(issues, "circular_json_value", path, "must not contain circular references");
    return;
  }

  ancestors.add(value);
  if (Array.isArray(value)) {
    value.forEach((entry, index) => {
      validateJsonValueInternal(entry, `${path}[${index}]`, issues, ancestors);
    });
  } else {
    const prototype = Object.getPrototypeOf(value) as object | null;
    if (prototype !== Object.prototype && prototype !== null) {
      addIssue(issues, "non_plain_json_object", path, "must be a plain JSON object");
    } else {
      for (const [key, entry] of Object.entries(value)) {
        validateJsonValueInternal(entry, `${path}.${key}`, issues, ancestors);
      }
    }
  }
  ancestors.delete(value);
};

export const validateJsonValue = (
  value: unknown,
  path = "value",
): readonly ValidationIssue[] => {
  const issues: ValidationIssue[] = [];
  validateJsonValueInternal(value, path, issues, new Set());
  return issues;
};

const findDuplicates = <T>(values: readonly T[]): readonly T[] => {
  const seen = new Set<T>();
  const duplicates = new Set<T>();
  for (const value of values) {
    if (seen.has(value)) {
      duplicates.add(value);
    }
    seen.add(value);
  }
  return [...duplicates];
};

export const validateWidgetThemeDeclaration = (
  declaration: WidgetThemeDeclaration,
  path = "theme",
): readonly ValidationIssue[] => {
  const issues: ValidationIssue[] = [];

  if (declaration.contractVersion !== THEME_CONTRACT_VERSION) {
    addIssue(
      issues,
      "unsupported_theme_contract",
      `${path}.contractVersion`,
      `must equal Theme Contract ${THEME_CONTRACT_VERSION}`,
    );
  }

  const supports = declaration.supports as readonly string[];
  for (const support of supports) {
    if (!THEME_SUPPORT.has(support as WidgetThemeSupport)) {
      addIssue(
        issues,
        "unknown_theme_support",
        `${path}.supports`,
        `contains unsupported mode ${JSON.stringify(support)}`,
      );
    }
  }
  if (findDuplicates(supports).length > 0) {
    addIssue(issues, "duplicate_theme_support", `${path}.supports`, "must not contain duplicates");
  }

  if (declaration.conformance === "standard") {
    for (const required of STANDARD_WIDGET_THEME_SUPPORT) {
      if (!supports.includes(required)) {
        addIssue(
          issues,
          "missing_theme_support",
          `${path}.supports`,
          `standard widgets must support ${required}`,
        );
      }
    }
  }

  declaration.exceptions?.forEach((exception, index) => {
    if (exception.reason.trim().length === 0) {
      addIssue(
        issues,
        "missing_theme_exception_reason",
        `${path}.exceptions[${index}].reason`,
        "must explain why the fixed brand or media color is necessary",
      );
    }
  });

  return issues;
};

export const widgetSatisfiesSlotRole = (
  widget: WidgetDefinition,
  slot: DefaultWidgetSlot,
  roleContracts?: ReadonlyMap<WidgetRoleId, WidgetRoleContract>,
): boolean => {
  const rule = slot.allowedSubstitution;
  const acceptedRoles = new Set([
    slot.requiredRole,
    ...(rule?.compatibleRoleIds ?? []),
  ]);
  const providesAcceptedRole = widget.providesRoles.some((roleId) => {
    if (!acceptedRoles.has(roleId)) return false;
    if (roleContracts === undefined) return true;
    const role = roleContracts.get(roleId);
    return role !== undefined && widgetSatisfiesRoleContract(widget, role);
  });
  const allowedPublisher =
    rule?.allowedPublisherAppIds === undefined ||
    rule.allowedPublisherAppIds.includes(widget.publisherAppId);
  const allowedVersion =
    rule?.minimumDefinitionVersion === undefined ||
    widget.definitionVersion >= rule.minimumDefinitionVersion;
  return providesAcceptedRole && allowedPublisher && allowedVersion;
};

const schemaReferencesEqual = (
  left: JsonSchemaReference,
  right: JsonSchemaReference,
): boolean => left.schemaId === right.schemaId && left.version === right.version;

/** A claimed role is executable compatibility, not merely a matching label. */
export const widgetSatisfiesRoleContract = (
  widget: WidgetDefinition,
  role: WidgetRoleContract,
): boolean =>
  (role.inputSchema === undefined ||
    schemaReferencesEqual(widget.inputSchema, role.inputSchema)) &&
  (role.outputIntentSchemas ?? []).every((requiredIntent) =>
    widget.outputIntentSchemas.some((providedIntent) =>
      schemaReferencesEqual(providedIntent, requiredIntent),
    ),
  );

const validateWidgetSize = (
  widget: WidgetDefinition,
  path: string,
  issues: ValidationIssue[],
): void => {
  const { min, max, default: defaultSize, modes } = widget.sizeContract;
  for (const [name, size] of Object.entries({ min, max, default: defaultSize })) {
    if (size === undefined) {
      continue;
    }
    if (!isPositiveInteger(size.w) || !isPositiveInteger(size.h)) {
      addIssue(
        issues,
        "invalid_widget_size",
        `${path}.sizeContract.${name}`,
        "width and height must be positive integers",
      );
    }
  }
  if (defaultSize.w < min.w || defaultSize.h < min.h) {
    addIssue(
      issues,
      "default_below_minimum",
      `${path}.sizeContract.default`,
      "default size must not be smaller than the minimum",
    );
  }
  if (
    max !== undefined &&
    (max.w < min.w || max.h < min.h || defaultSize.w > max.w || defaultSize.h > max.h)
  ) {
    addIssue(
      issues,
      "invalid_size_range",
      `${path}.sizeContract`,
      "maximum must be at least the minimum and contain the default size",
    );
  }
  if (modes.length === 0 || findDuplicates(modes).length > 0) {
    addIssue(
      issues,
      "invalid_size_modes",
      `${path}.sizeContract.modes`,
      "must contain at least one unique size mode",
    );
  }
};

const validateWidgetDefinition = (
  widget: WidgetDefinition,
  app: AppContribution,
  path: string,
  roles: ReadonlyMap<WidgetRoleId, WidgetRoleContract>,
  modules: ReadonlyMap<WidgetModuleId, WidgetModule>,
  issues: ValidationIssue[],
): void => {
  validateNamespacedId(widget.typeId, `${path}.typeId`, issues);
  validateNamespacedId(widget.rendererModuleId, `${path}.rendererModuleId`, issues);
  if (widget.publisherAppId !== app.appId) {
    addIssue(
      issues,
      "widget_owner_mismatch",
      `${path}.publisherAppId`,
      "must equal the contribution appId",
    );
  }
  if (!isPositiveInteger(widget.definitionVersion)) {
    addIssue(
      issues,
      "invalid_definition_version",
      `${path}.definitionVersion`,
      "must be a positive integer",
    );
  }
  if (widget.help !== undefined) {
    validateHelpContent(widget.help, `${path}.help`, issues);
  }
  const outputSchemaKeys = widget.outputIntentSchemas.map(
    (schema) => `${schema.schemaId}@${schema.version}`,
  );
  if (findDuplicates(outputSchemaKeys).length > 0) {
    addIssue(
      issues,
      "duplicate_widget_output_intent",
      `${path}.outputIntentSchemas`,
      "must not contain duplicate intent schemas",
    );
  }
  const effectDeclarations = widget.outputIntentEffects ?? [];
  effectDeclarations.forEach((declaration, declarationIndex) => {
    if (!WIDGET_INTENT_EFFECTS.has(declaration.effect)) {
      addIssue(
        issues,
        "unknown_widget_intent_effect_kind",
        `${path}.outputIntentEffects[${declarationIndex}].effect`,
        "must be read, mutation, navigation, or external",
      );
    }
    if (!WIDGET_INTENT_PREVIEW_POLICIES.has(declaration.preview)) {
      addIssue(
        issues,
        "unknown_widget_intent_preview_policy",
        `${path}.outputIntentEffects[${declarationIndex}].preview`,
        "must be simulate or block",
      );
    }
  });
  const effectSchemaKeys = effectDeclarations.map(
    (declaration) => `${declaration.schema.schemaId}@${declaration.schema.version}`,
  );
  if (findDuplicates(effectSchemaKeys).length > 0) {
    addIssue(
      issues,
      "duplicate_widget_intent_effect",
      `${path}.outputIntentEffects`,
      "must contain exactly one effect declaration per output intent",
    );
  }
  outputSchemaKeys.forEach((schemaKey, schemaIndex) => {
    if (!effectSchemaKeys.includes(schemaKey)) {
      addIssue(
        issues,
        "missing_widget_intent_effect",
        `${path}.outputIntentSchemas[${schemaIndex}]`,
        "must have a matching semantic effect and preview policy",
      );
    }
  });
  effectSchemaKeys.forEach((schemaKey, declarationIndex) => {
    if (!outputSchemaKeys.includes(schemaKey)) {
      addIssue(
        issues,
        "unknown_widget_intent_effect",
        `${path}.outputIntentEffects[${declarationIndex}]`,
        "must reference an output intent schema declared by this widget",
      );
    }
  });
  const draftNames = widget.drafts?.map((draft) => draft.draftName) ?? [];
  if (findDuplicates(draftNames).length > 0) {
    addIssue(
      issues,
      "duplicate_widget_draft",
      `${path}.drafts`,
      "must not contain duplicate draft names",
    );
  }
  widget.drafts?.forEach((draft, draftIndex) => {
    const draftPath = `${path}.drafts[${draftIndex}]`;
    if (!/^[a-z][a-z0-9_-]*$/.test(draft.draftName)) {
      addIssue(
        issues,
        "invalid_widget_draft_name",
        `${draftPath}.draftName`,
        "must be a lowercase local identifier",
      );
    }
    if (!isPositiveInteger(draft.schema.version)) {
      addIssue(
        issues,
        "invalid_widget_draft_schema",
        `${draftPath}.schema.version`,
        "must be a positive integer",
      );
    }
    if (!isPositiveInteger(draft.maxBytes)) {
      addIssue(
        issues,
        "invalid_widget_draft_size",
        `${draftPath}.maxBytes`,
        "must be a positive integer",
      );
    }
    if (
      draft.retentionDays !== undefined &&
      !isPositiveInteger(draft.retentionDays)
    ) {
      addIssue(
        issues,
        "invalid_widget_draft_retention",
        `${draftPath}.retentionDays`,
        "must be a positive integer when provided",
      );
    }
    if (draft.sensitivity === "secret" && draft.persistence !== "none") {
      addIssue(
        issues,
        "secret_widget_draft_persisted",
        `${draftPath}.persistence`,
        "secret drafts must use persistence none",
      );
    }
    if (draft.scope.kind === "input-field" && draft.scope.path.length === 0) {
      addIssue(
        issues,
        "invalid_widget_draft_scope",
        `${draftPath}.scope.path`,
        "input-field scope requires a non-empty path",
      );
    }
  });
  if (widget.libraryPath.length === 0 || widget.libraryPath.some((part) => part.trim() === "")) {
    addIssue(
      issues,
      "invalid_library_path",
      `${path}.libraryPath`,
      "must contain non-empty browsing labels",
    );
  }
  if (widget.providesRoles.length === 0) {
    addIssue(
      issues,
      "missing_widget_role",
      `${path}.providesRoles`,
      "must declare at least one functional role",
    );
  }
  if (findDuplicates(widget.providesRoles).length > 0) {
    addIssue(issues, "duplicate_widget_role", `${path}.providesRoles`, "must not contain duplicates");
  }
  widget.providesRoles.forEach((roleId, roleIndex) => {
    validateRoleId(roleId, `${path}.providesRoles[${roleIndex}]`, issues);
    const role = roles.get(roleId);
    if (role === undefined) {
      addIssue(
        issues,
        "unknown_widget_role",
        `${path}.providesRoles[${roleIndex}]`,
        `references unregistered role ${roleId}`,
      );
      return;
    }
    if (
      role.inputSchema !== undefined &&
      !schemaReferencesEqual(widget.inputSchema, role.inputSchema)
    ) {
      addIssue(
        issues,
        "widget_role_input_schema_mismatch",
        `${path}.inputSchema`,
        `must exactly match input schema ${role.inputSchema.schemaId}@${role.inputSchema.version} required by ${roleId}`,
      );
    }
    (role.outputIntentSchemas ?? []).forEach((requiredIntent) => {
      const providesIntent = widget.outputIntentSchemas.some((providedIntent) =>
        schemaReferencesEqual(providedIntent, requiredIntent),
      );
      if (!providesIntent) {
        addIssue(
          issues,
          "widget_role_output_intent_missing",
          `${path}.outputIntentSchemas`,
          `must provide output intent ${requiredIntent.schemaId}@${requiredIntent.version} required by ${roleId}`,
        );
      }
    });
  });
  validateWidgetSize(widget, path, issues);
  issues.push(...validateWidgetThemeDeclaration(widget.theme, `${path}.theme`));

  // A durable widget is a single keep-alive instance, so more than one per view
  // is meaningless and it cannot use the host-owned draft machinery, which would
  // re-key and remount the very element the durable lifecycle keeps alive.
  if (widget.durable === true && widget.multiplicity !== "single_per_view") {
    addIssue(
      issues,
      "durable_widget_multiplicity",
      `${path}.multiplicity`,
      "a durable widget is one keep-alive instance and must be single_per_view",
    );
  }
  if (widget.durable === true && (widget.drafts?.length ?? 0) > 0) {
    addIssue(
      issues,
      "durable_widget_drafts",
      `${path}.drafts`,
      "a durable widget owns its own persistence and keeps its own live state, so it must not declare host drafts",
    );
  }

  const module = modules.get(widget.rendererModuleId);
  if (module === undefined) {
    addIssue(
      issues,
      "missing_renderer_module",
      `${path}.rendererModuleId`,
      `has no registered lazy module ${widget.rendererModuleId}`,
    );
  } else if (module.widgetTypeId !== widget.typeId) {
    addIssue(
      issues,
      "renderer_widget_mismatch",
      `${path}.rendererModuleId`,
      `module is bound to ${module.widgetTypeId}, not ${widget.typeId}`,
    );
  }
};

const placementOverlaps = (
  left: DefaultWidgetSlot,
  right: DefaultWidgetSlot,
): boolean => {
  const a = left.defaultLayout;
  const b = right.defaultLayout;
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
};

const validateOrder = (
  order: readonly string[],
  slots: readonly DefaultWidgetSlot[],
  path: string,
  requireAllSlots: boolean,
  issues: ValidationIssue[],
): void => {
  const slotIds = new Set(slots.map((slot) => slot.slotId));
  const requiredIds = slots
    .filter((slot) => requireAllSlots || slot.presence !== "default_off")
    .map((slot) => slot.slotId);
  const duplicates = findDuplicates(order);
  if (duplicates.length > 0) {
    addIssue(issues, "duplicate_order_entry", path, `duplicates ${duplicates.join(", ")}`);
  }
  order.forEach((slotId, index) => {
    if (!slotIds.has(slotId as DefaultWidgetSlot["slotId"])) {
      addIssue(
        issues,
        "unknown_order_slot",
        `${path}[${index}]`,
        `references unknown slot ${slotId}`,
      );
    }
  });
  for (const slotId of requiredIds) {
    if (!order.includes(slotId)) {
      addIssue(issues, "missing_order_slot", path, `must include slot ${slotId}`);
    }
  }
};

const validateSurfaceRegion = (
  region: SurfaceRegionDefinition,
  path: string,
  roles: ReadonlyMap<WidgetRoleId, WidgetRoleContract>,
  issues: ValidationIssue[],
): void => {
  if (!REGION_ID.test(region.regionId)) {
    addIssue(
      issues,
      "invalid_region_id",
      `${path}.regionId`,
      "must be a stable local purpose ID such as editor",
    );
  }
  validateRoleId(region.role, `${path}.role`, issues);
  if (!roles.has(region.role)) {
    addIssue(
      issues,
      "unknown_region_role",
      `${path}.role`,
      `references unregistered role ${region.role}`,
    );
  }
  if (region.presence !== "required") {
    addIssue(
      issues,
      "invalid_region_presence",
      `${path}.presence`,
      "a single-surface region is structural and must declare presence required",
    );
  }
  validateHelpContent(region.help, `${path}.help`, issues);
  issues.push(...validateWidgetThemeDeclaration(region.theme, `${path}.theme`));
};

/**
 * A single-surface view keeps every identity, trust, route, and theme invariant but
 * has no grid. It must carry a non-empty surface composition of versioned, themed
 * regions and must leave the grid slot and order fields empty.
 */
const validateSingleSurfaceView = (
  view: ViewDefinition,
  path: string,
  roles: ReadonlyMap<WidgetRoleId, WidgetRoleContract>,
  issues: ValidationIssue[],
): void => {
  if (view.defaultSlots.length > 0) {
    addIssue(
      issues,
      "single_surface_has_grid_slots",
      `${path}.defaultSlots`,
      "a single-surface view composes regions itself and must not declare grid slots",
    );
  }
  if (view.readingOrder.length > 0 || view.mobileOrder.length > 0) {
    addIssue(
      issues,
      "single_surface_has_grid_order",
      `${path}.readingOrder`,
      "a single-surface view has no grid reading or mobile order",
    );
  }

  const surface = view.surface;
  if (surface === undefined) {
    addIssue(
      issues,
      "missing_surface_composition",
      `${path}.surface`,
      "a single-surface view must declare its surface region composition",
    );
    return;
  }

  if (surface.regions.length === 0) {
    addIssue(
      issues,
      "empty_surface_composition",
      `${path}.surface.regions`,
      "must declare at least one surface region",
    );
  }
  const regionIds = surface.regions.map((region) => region.regionId);
  if (findDuplicates(regionIds).length > 0) {
    addIssue(
      issues,
      "duplicate_region_id",
      `${path}.surface.regions`,
      "region IDs must be unique per view",
    );
  }
  const roleIds = surface.regions.map((region) => region.role);
  if (findDuplicates(roleIds).length > 0) {
    addIssue(
      issues,
      "duplicate_region_role",
      `${path}.surface.regions`,
      "each surface region must fill a distinct role",
    );
  }
  surface.regions.forEach((region, index) => {
    validateSurfaceRegion(region, `${path}.surface.regions[${index}]`, roles, issues);
  });
};

const validateViewDefinition = (
  view: ViewDefinition,
  app: AppContribution,
  path: string,
  widgets: ReadonlyMap<WidgetTypeId, WidgetDefinition>,
  roles: ReadonlyMap<WidgetRoleId, WidgetRoleContract>,
  issues: ValidationIssue[],
): void => {
  validateNamespacedId(view.viewId, `${path}.viewId`, issues);
  if (view.ownerAppId !== app.appId) {
    addIssue(
      issues,
      "view_owner_mismatch",
      `${path}.ownerAppId`,
      "must equal the contribution appId",
    );
  }
  if (!isPositiveInteger(view.definitionVersion)) {
    addIssue(
      issues,
      "invalid_definition_version",
      `${path}.definitionVersion`,
      "must be a positive integer",
    );
  }
  if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(view.route)) {
    addIssue(
      issues,
      "invalid_view_route",
      `${path}.route`,
      "must be one normalized dashboard route segment without slash, query, hash, or traversal",
    );
  }
  if (view.navigation.label.trim() === "") {
    addIssue(
      issues,
      "missing_navigation_label",
      `${path}.navigation.label`,
      "must provide a non-empty navigation label",
    );
  }
  if (!Number.isInteger(view.navigation.order) || view.navigation.order < 0) {
    addIssue(
      issues,
      "invalid_navigation_order",
      `${path}.navigation.order`,
      "must be a non-negative integer",
    );
  }
  if (view.navigation.isDefault === true && view.navigation.hidden === true) {
    addIssue(
      issues,
      "hidden_default_view",
      `${path}.navigation`,
      "the default dashboard view cannot be hidden from navigation",
    );
  }
  if (view.settings !== undefined) {
    validateNamespacedId(view.settings.pageId, `${path}.settings.pageId`, issues);
    if (view.settings.label.trim() === "") {
      addIssue(
        issues,
        "missing_view_settings_label",
        `${path}.settings.label`,
        "must provide a contextual accessible label such as Journal settings",
      );
    }
  }
  const layoutKind: ViewLayoutKind = view.layoutKind ?? "standard-grid";
  if (view.layoutKind !== undefined && !LAYOUT_KINDS.has(view.layoutKind)) {
    addIssue(
      issues,
      "invalid_layout_kind",
      `${path}.layoutKind`,
      "must be standard-grid or single-surface",
    );
  }

  // The single-surface kind relaxes ONLY the grid-composition invariants (RGL columns,
  // slot placement, overlap, reading and mobile order). Every identity, trust, route,
  // and theme safety invariant above and in the region validation below is retained.
  if (layoutKind === "single-surface") {
    validateSingleSurfaceView(view, path, roles, issues);
    return;
  }

  if (view.surface !== undefined) {
    addIssue(
      issues,
      "surface_on_standard_grid",
      `${path}.surface`,
      "only a single-surface view may declare a surface composition",
    );
  }

  if (view.grid.columns !== DASHBOARD_GRID_COLUMNS) {
    addIssue(
      issues,
      "invalid_grid_columns",
      `${path}.grid.columns`,
      `standard views use the ${DASHBOARD_GRID_COLUMNS}-column dashboard grid`,
    );
  }

  const slotIds = view.defaultSlots.map((slot) => slot.slotId);
  const instanceIds = view.defaultSlots.map((slot) => slot.defaultInstanceId);
  if (findDuplicates(slotIds).length > 0) {
    addIssue(issues, "duplicate_slot_id", `${path}.defaultSlots`, "slot IDs must be unique per view");
  }
  if (findDuplicates(instanceIds).length > 0) {
    addIssue(
      issues,
      "duplicate_instance_id",
      `${path}.defaultSlots`,
      "default instance IDs must be unique per view",
    );
  }

  view.defaultSlots.forEach((slot, index) => {
    const slotPath = `${path}.defaultSlots[${index}]`;
    if (!SLOT_ID.test(slot.slotId)) {
      addIssue(
        issues,
        "invalid_slot_id",
        `${slotPath}.slotId`,
        "must be a stable local purpose ID such as capture",
      );
    }
    validateHelpContent(slot.help, `${slotPath}.help`, issues);
    if (!INSTANCE_ID.test(slot.defaultInstanceId)) {
      addIssue(
        issues,
        "invalid_instance_id",
        `${slotPath}.defaultInstanceId`,
        "must be an opaque stable instance ID using letters, numbers, colon, underscore, or hyphen",
      );
    }
    validateRoleId(slot.requiredRole, `${slotPath}.requiredRole`, issues);
    if (!roles.has(slot.requiredRole)) {
      addIssue(
        issues,
        "unknown_required_role",
        `${slotPath}.requiredRole`,
        `references unregistered role ${slot.requiredRole}`,
      );
    }
    slot.allowedSubstitution?.compatibleRoleIds?.forEach((roleId, roleIndex) => {
      if (!roles.has(roleId)) {
        addIssue(
          issues,
          "unknown_compatible_role",
          `${slotPath}.allowedSubstitution.compatibleRoleIds[${roleIndex}]`,
          `references unregistered role ${roleId}`,
        );
      }
    });
    if (
      slot.allowedSubstitution?.minimumDefinitionVersion !== undefined &&
      !isPositiveInteger(slot.allowedSubstitution.minimumDefinitionVersion)
    ) {
      addIssue(
        issues,
        "invalid_minimum_definition_version",
        `${slotPath}.allowedSubstitution.minimumDefinitionVersion`,
        "must be a positive integer",
      );
    }

    if (slot.presence === "required" && slot.lockedReason?.trim() === "") {
      addIssue(
        issues,
        "missing_required_reason",
        `${slotPath}.lockedReason`,
        "required presence must have a plain-language invariant reason",
      );
    } else if (slot.presence === "required" && slot.lockedReason === undefined) {
      addIssue(
        issues,
        "missing_required_reason",
        `${slotPath}.lockedReason`,
        "required presence must have a plain-language invariant reason",
      );
    } else if (slot.presence !== "required" && slot.lockedReason !== undefined) {
      addIssue(
        issues,
        "optional_slot_locked",
        `${slotPath}.lockedReason`,
        "only a required slot may lock presence",
      );
    }

    issues.push(...validateJsonValue(slot.defaultSettings, `${slotPath}.defaultSettings`));
    if (slot.defaultBindings !== undefined) {
      issues.push(...validateJsonValue(slot.defaultBindings, `${slotPath}.defaultBindings`));
    }

    const layout = slot.defaultLayout;
    if (
      !Number.isInteger(layout.x) ||
      !Number.isInteger(layout.y) ||
      layout.x < 0 ||
      layout.y < 0 ||
      !isPositiveInteger(layout.w) ||
      !isPositiveInteger(layout.h) ||
      layout.x + layout.w > view.grid.columns
    ) {
      addIssue(
        issues,
        "invalid_default_layout",
        `${slotPath}.defaultLayout`,
        `must be a positive integer placement within ${view.grid.columns} columns`,
      );
    }

    const widget = widgets.get(slot.defaultWidgetTypeId);
    if (widget === undefined) {
      addIssue(
        issues,
        "unknown_default_widget",
        `${slotPath}.defaultWidgetTypeId`,
        `references unregistered widget type ${slot.defaultWidgetTypeId}`,
      );
      return;
    }
    if (!widgetSatisfiesSlotRole(widget, slot, roles)) {
      addIssue(
        issues,
        "incompatible_default_widget",
        `${slotPath}.defaultWidgetTypeId`,
        `widget ${widget.typeId} does not satisfy role ${slot.requiredRole}`,
      );
    }
    const { min, max } = widget.sizeContract;
    if (
      layout.w < min.w ||
      layout.h < min.h ||
      (max !== undefined && (layout.w > max.w || layout.h > max.h))
    ) {
      addIssue(
        issues,
        "layout_outside_widget_size",
        `${slotPath}.defaultLayout`,
        `must fit ${widget.typeId}'s declared size contract`,
      );
    }
  });

  for (let left = 0; left < view.defaultSlots.length; left += 1) {
    for (let right = left + 1; right < view.defaultSlots.length; right += 1) {
      const leftSlot = view.defaultSlots[left];
      const rightSlot = view.defaultSlots[right];
      if (leftSlot !== undefined && rightSlot !== undefined && placementOverlaps(leftSlot, rightSlot)) {
        addIssue(
          issues,
          "overlapping_default_layout",
          `${path}.defaultSlots`,
          `default layouts for ${leftSlot.slotId} and ${rightSlot.slotId} overlap`,
        );
      }
    }
  }

  validateOrder(view.readingOrder, view.defaultSlots, `${path}.readingOrder`, true, issues);
  validateOrder(view.mobileOrder, view.defaultSlots, `${path}.mobileOrder`, false, issues);
};

/** Validates both contribution-local invariants and references into an existing registry. */
export const validateAppContribution = (
  contribution: AppContribution,
  context: ContributionValidationContext = {},
): readonly ValidationIssue[] => {
  const issues: ValidationIssue[] = [];
  validateNamespacedId(contribution.appId, "appId", issues);
  if (contribution.schemaVersion !== 1) {
    addIssue(issues, "unsupported_schema_version", "schemaVersion", "must equal 1");
  }
  if (!isPositiveInteger(contribution.definitionVersion)) {
    addIssue(
      issues,
      "invalid_definition_version",
      "definitionVersion",
      "must be a positive integer",
    );
  }
  if (context.appIds?.has(contribution.appId) === true) {
    addIssue(issues, "duplicate_app_id", "appId", `app ${contribution.appId} is already registered`);
  }

  const localRoleIds = contribution.widgetRoles.map((role) => role.roleId);
  const localWidgetIds = contribution.widgetDefinitions.map((widget) => widget.typeId);
  const localViewIds = contribution.views.map((view) => view.viewId);
  const localDefaultViews = contribution.views.filter(
    (view) => view.navigation.isDefault === true,
  );
  for (const [code, path, duplicates] of [
    ["duplicate_role_id", "widgetRoles", findDuplicates(localRoleIds)],
    ["duplicate_widget_type_id", "widgetDefinitions", findDuplicates(localWidgetIds)],
    ["duplicate_view_id", "views", findDuplicates(localViewIds)],
  ] as const) {
    if (duplicates.length > 0) {
      addIssue(issues, code, path, `duplicates ${duplicates.join(", ")}`);
    }
  }
  if (localDefaultViews.length > 1) {
    addIssue(
      issues,
      "duplicate_default_view",
      "views",
      "an App contribution cannot nominate more than one default dashboard view",
    );
  }
  const existingDefault = [...(context.viewDefinitions?.values() ?? [])].find(
    (view) => view.navigation.isDefault === true,
  );
  if (localDefaultViews.length === 1 && existingDefault !== undefined) {
    addIssue(
      issues,
      "duplicate_default_view",
      "views",
      `default dashboard view ${existingDefault.viewId} is already registered`,
    );
  }

  const roles = new Map(context.widgetRoles ?? []);
  contribution.widgetRoles.forEach((role, index) => {
    const path = `widgetRoles[${index}]`;
    validateRoleId(role.roleId, `${path}.roleId`, issues);
    if (role.ownerAppId !== contribution.appId) {
      addIssue(
        issues,
        "role_owner_mismatch",
        `${path}.ownerAppId`,
        "must equal the contribution appId",
      );
    }
    if (roles.has(role.roleId)) {
      addIssue(issues, "duplicate_role_id", `${path}.roleId`, `role ${role.roleId} is already registered`);
    } else {
      roles.set(role.roleId, role);
    }
  });

  const widgets = new Map(context.widgetDefinitions ?? []);
  contribution.widgetDefinitions.forEach((widget, index) => {
    if (widgets.has(widget.typeId)) {
      addIssue(
        issues,
        "duplicate_widget_type_id",
        `widgetDefinitions[${index}].typeId`,
        `widget type ${widget.typeId} is already registered`,
      );
    } else {
      widgets.set(widget.typeId, widget);
    }
  });

  const modules = context.widgetModules ?? new Map<WidgetModuleId, WidgetModule>();
  contribution.widgetDefinitions.forEach((widget, index) => {
    validateWidgetDefinition(widget, contribution, `widgetDefinitions[${index}]`, roles, modules, issues);
  });

  contribution.views.forEach((view, index) => {
    const path = `views[${index}]`;
    if (context.viewDefinitions?.has(view.viewId) === true) {
      addIssue(issues, "duplicate_view_id", `${path}.viewId`, `view ${view.viewId} is already registered`);
    }
    if (context.routes?.has(view.route) === true) {
      addIssue(issues, "duplicate_view_route", `${path}.route`, `route ${view.route} is already registered`);
    }
    validateViewDefinition(view, contribution, path, widgets, roles, issues);
  });

  return issues;
};

export const assertValidAppContribution = (
  contribution: AppContribution,
  context: ContributionValidationContext = {},
): void => {
  const issues = validateAppContribution(contribution, context);
  if (issues.length > 0) {
    throw new ContributionValidationError(issues);
  }
};
