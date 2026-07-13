import type {
  AppContribution,
  AppId,
  LoadedWidgetModule,
  ViewDefinition,
  ViewId,
  WidgetDefinition,
  WidgetModule,
  WidgetModuleId,
  WidgetRoleContract,
  WidgetRoleId,
  WidgetTypeId,
} from "./contracts";
import type {
  LoadedStandardWidgetViewModule,
  StandardWidgetViewModule,
  ViewModule,
} from "./viewModules";
import {
  ContributionValidationError,
  assertValidAppContribution,
  isNamespacedDashboardId,
  type ValidationIssue,
} from "./validate";

export interface RegisteredView {
  readonly app: AppContribution;
  readonly definition: ViewDefinition;
  readonly trust: ContributionTrustProvenance;
}

export interface RegisteredWidget {
  readonly app: AppContribution;
  readonly definition: WidgetDefinition;
  readonly module: WidgetModule;
  readonly trust: ContributionTrustProvenance;
}

export interface RegisteredViewModule {
  readonly app: AppContribution;
  readonly definition: ViewDefinition;
  readonly module: StandardWidgetViewModule;
  readonly trust: ContributionTrustProvenance;
}

export type ContributionTrustProvenance =
  | "native"
  | "verified"
  | "personal"
  | "developer"
  | "unverified";

export interface AppRegistrationOptions {
  /** Assigned by the trusted installer/bootstrap caller; never inferred from IDs. */
  readonly trust?: ContributionTrustProvenance;
}

export interface RegistrationReceipt {
  readonly appId: AppId;
  readonly viewIds: readonly ViewId[];
  readonly widgetTypeIds: readonly WidgetTypeId[];
  readonly trust: ContributionTrustProvenance;
}

export class UnknownContributionError extends Error {
  constructor(
    kind: "app" | "view" | "view-module" | "widget" | "role",
    id: string,
  ) {
    super(`Unknown dashboard ${kind}: ${id}`);
    this.name = "UnknownContributionError";
  }
}

/**
 * In-memory metadata registry. Registration is validated and atomic: no partial App is
 * observable when any definition, reference, role, module, or layout is invalid.
 */
export class ContributionRegistry {
  readonly #apps = new Map<AppId, AppContribution>();
  readonly #views = new Map<ViewId, ViewDefinition>();
  readonly #widgets = new Map<WidgetTypeId, WidgetDefinition>();
  readonly #roles = new Map<WidgetRoleId, WidgetRoleContract>();
  readonly #modules = new Map<WidgetModuleId, WidgetModule>();
  readonly #widgetModules = new Map<WidgetTypeId, WidgetModule>();
  readonly #viewModules = new Map<ViewId, StandardWidgetViewModule>();
  readonly #moduleIds = new Set<string>();
  readonly #trust = new Map<AppId, ContributionTrustProvenance>();
  readonly #routes = new Set<string>();

  registerApp(
    contribution: AppContribution,
    widgetModules: readonly WidgetModule[],
    viewModules: readonly ViewModule[] = [],
    options: AppRegistrationOptions = {},
  ): RegistrationReceipt {
    const trust = options.trust ?? "unverified";
    const moduleIssues = [
      ...this.#validateModules(contribution, widgetModules),
      ...this.#validateViewModules(contribution, viewModules, widgetModules),
    ];
    if (moduleIssues.length > 0) {
      throw new ContributionValidationError(moduleIssues);
    }

    const candidateModules = new Map(this.#modules);
    widgetModules.forEach((module) => candidateModules.set(module.moduleId, module));
    assertValidAppContribution(contribution, {
      appIds: new Set(this.#apps.keys()),
      viewDefinitions: this.#views,
      widgetDefinitions: this.#widgets,
      widgetRoles: this.#roles,
      widgetModules: candidateModules,
      routes: this.#routes,
    });

    this.#apps.set(contribution.appId, contribution);
    this.#trust.set(contribution.appId, trust);
    contribution.widgetRoles.forEach((role) => this.#roles.set(role.roleId, role));
    contribution.widgetDefinitions.forEach((widget) =>
      this.#widgets.set(widget.typeId, widget),
    );
    contribution.views.forEach((view) => {
      this.#views.set(view.viewId, view);
      this.#routes.add(view.route);
    });
    widgetModules.forEach((module) => {
      this.#modules.set(module.moduleId, module);
      this.#widgetModules.set(module.widgetTypeId, module);
      this.#moduleIds.add(module.moduleId);
    });
    viewModules.forEach((module) => {
      if (module.kind !== "standard-widget-view") return;
      this.#viewModules.set(module.viewId, module);
      this.#moduleIds.add(module.moduleId);
    });

    return {
      appId: contribution.appId,
      viewIds: contribution.views.map((view) => view.viewId),
      widgetTypeIds: contribution.widgetDefinitions.map((widget) => widget.typeId),
      trust,
    };
  }

  getApp(appId: AppId): AppContribution | undefined {
    return this.#apps.get(appId);
  }

  requireApp(appId: AppId): AppContribution {
    const app = this.getApp(appId);
    if (app === undefined) {
      throw new UnknownContributionError("app", appId);
    }
    return app;
  }

  getView(viewId: ViewId): RegisteredView | undefined {
    const definition = this.#views.get(viewId);
    if (definition === undefined) {
      return undefined;
    }
    return {
      app: this.requireApp(definition.ownerAppId),
      definition,
      trust: this.requireAppTrust(definition.ownerAppId),
    };
  }

  requireView(viewId: ViewId): RegisteredView {
    const view = this.getView(viewId);
    if (view === undefined) {
      throw new UnknownContributionError("view", viewId);
    }
    return view;
  }

  getViewByRoute(route: string): RegisteredView | undefined {
    const definition = [...this.#views.values()].find((view) => view.route === route);
    return definition === undefined ? undefined : this.getView(definition.viewId);
  }

  getWidget(widgetTypeId: WidgetTypeId): RegisteredWidget | undefined {
    const definition = this.#widgets.get(widgetTypeId);
    const module = this.#widgetModules.get(widgetTypeId);
    if (definition === undefined || module === undefined) {
      return undefined;
    }
    return {
      app: this.requireApp(definition.publisherAppId),
      definition,
      module,
      trust: this.requireAppTrust(definition.publisherAppId),
    };
  }

  requireWidget(widgetTypeId: WidgetTypeId): RegisteredWidget {
    const widget = this.getWidget(widgetTypeId);
    if (widget === undefined) {
      throw new UnknownContributionError("widget", widgetTypeId);
    }
    return widget;
  }

  getRole(roleId: WidgetRoleId): WidgetRoleContract | undefined {
    return this.#roles.get(roleId);
  }

  requireRole(roleId: WidgetRoleId): WidgetRoleContract {
    const role = this.getRole(roleId);
    if (role === undefined) {
      throw new UnknownContributionError("role", roleId);
    }
    return role;
  }

  getAppTrust(appId: AppId): ContributionTrustProvenance | undefined {
    return this.#trust.get(appId);
  }

  requireAppTrust(appId: AppId): ContributionTrustProvenance {
    const trust = this.getAppTrust(appId);
    if (trust === undefined) {
      throw new UnknownContributionError("app", appId);
    }
    return trust;
  }

  listApps(): readonly AppContribution[] {
    return [...this.#apps.values()];
  }

  listViews(): readonly RegisteredView[] {
    return [...this.#views.values()]
      .sort(
        (left, right) =>
          left.navigation.order - right.navigation.order ||
          left.navigation.label.localeCompare(right.navigation.label),
      )
      .map((definition) => this.requireView(definition.viewId));
  }

  listWidgets(): readonly RegisteredWidget[] {
    return [...this.#widgets.keys()].map((widgetTypeId) =>
      this.requireWidget(widgetTypeId),
    );
  }

  async loadWidgetModule(widgetTypeId: WidgetTypeId): Promise<LoadedWidgetModule> {
    const loaded = await this.requireWidget(widgetTypeId).module.load();
    if (
      typeof loaded !== "object" ||
      loaded === null ||
      !("default" in loaded) ||
      loaded.default === undefined
    ) {
      throw new Error(`Widget module ${widgetTypeId} has no default renderer export`);
    }
    return loaded;
  }

  getViewModule(viewId: ViewId): RegisteredViewModule | undefined {
    const view = this.getView(viewId);
    const module = this.#viewModules.get(viewId);
    if (view === undefined || module === undefined) {
      return undefined;
    }
    return { ...view, module };
  }

  requireViewModule(viewId: ViewId): RegisteredViewModule {
    const module = this.getViewModule(viewId);
    if (module === undefined) {
      throw new UnknownContributionError("view-module", viewId);
    }
    return module;
  }

  async loadViewModule(viewId: ViewId): Promise<LoadedStandardWidgetViewModule> {
    const loaded = await this.requireViewModule(viewId).module.load();
    if (
      typeof loaded !== "object" ||
      loaded === null ||
      loaded.hostContractVersion !== 1 ||
      !("createRuntime" in loaded) ||
      typeof loaded.createRuntime !== "function"
    ) {
      throw new Error(
        `View module ${viewId} did not resolve the standard widget-view host contract`,
      );
    }
    return loaded;
  }

  #validateModules(
    contribution: AppContribution,
    widgetModules: readonly WidgetModule[],
  ): readonly ValidationIssue[] {
    const issues: ValidationIssue[] = [];
    const localWidgetIds = new Set(
      contribution.widgetDefinitions.map((widget) => widget.typeId),
    );
    const localModuleIds = new Set<WidgetModuleId>();
    const localModuleWidgetIds = new Set<WidgetTypeId>();

    widgetModules.forEach((module, index) => {
      const path = `widgetModules[${index}]`;
      if (!isNamespacedDashboardId(module.moduleId)) {
        issues.push({
          code: "invalid_namespaced_id",
          path: `${path}.moduleId`,
          message: "must be a lowercase namespaced module ID",
        });
      }
      if (!isNamespacedDashboardId(module.widgetTypeId)) {
        issues.push({
          code: "invalid_namespaced_id",
          path: `${path}.widgetTypeId`,
          message: "must be a lowercase namespaced widget type ID",
        });
      }
      if (
        localModuleIds.has(module.moduleId) ||
        this.#moduleIds.has(module.moduleId)
      ) {
        issues.push({
          code: "duplicate_widget_module_id",
          path: `${path}.moduleId`,
          message: `module ${module.moduleId} is already registered in this batch or registry`,
        });
      }
      if (localModuleWidgetIds.has(module.widgetTypeId)) {
        issues.push({
          code: "duplicate_widget_module_binding",
          path: `${path}.widgetTypeId`,
          message: `widget ${module.widgetTypeId} has more than one renderer module`,
        });
      }
      if (!localWidgetIds.has(module.widgetTypeId)) {
        issues.push({
          code: "orphan_widget_module",
          path: `${path}.widgetTypeId`,
          message: "must bind a widget definition from the same App contribution",
        });
      }
      if (typeof module.load !== "function") {
        issues.push({
          code: "invalid_widget_module_loader",
          path: `${path}.load`,
          message: "must be a lazy module loader function",
        });
      }
      localModuleIds.add(module.moduleId);
      localModuleWidgetIds.add(module.widgetTypeId);
    });

    return issues;
  }

  #validateViewModules(
    contribution: AppContribution,
    viewModules: readonly ViewModule[],
    widgetModules: readonly WidgetModule[],
  ): readonly ValidationIssue[] {
    const issues: ValidationIssue[] = [];
    const localViewIds = new Set(contribution.views.map((view) => view.viewId));
    const localModuleIds = new Set<string>(
      widgetModules.map((module) => module.moduleId),
    );
    const localModuleViewIds = new Set<ViewId>();

    viewModules.forEach((module, index) => {
      const path = `viewModules[${index}]`;
      if (!isNamespacedDashboardId(module.moduleId)) {
        issues.push({
          code: "invalid_namespaced_id",
          path: `${path}.moduleId`,
          message: "must be a lowercase namespaced module ID",
        });
      }
      if (!isNamespacedDashboardId(module.viewId)) {
        issues.push({
          code: "invalid_namespaced_id",
          path: `${path}.viewId`,
          message: "must bind a lowercase namespaced view ID",
        });
      }
      if (module.kind !== "standard-widget-view") {
        issues.push({
          code: "unsupported_view_module_kind",
          path: `${path}.kind`,
          message:
            "the standard registry only accepts standard-widget-view modules; developer roots require a separate trust-gated registry",
        });
      } else if (module.hostContractVersion !== 1) {
        issues.push({
          code: "unsupported_view_host_contract",
          path: `${path}.hostContractVersion`,
          message: "must target standard widget-view host contract version 1",
        });
      }
      if (localModuleIds.has(module.moduleId) || this.#moduleIds.has(module.moduleId)) {
        issues.push({
          code: "duplicate_view_module_id",
          path: `${path}.moduleId`,
          message: `module ${module.moduleId} is already registered in this batch or registry`,
        });
      }
      if (
        localModuleViewIds.has(module.viewId) ||
        this.#viewModules.has(module.viewId)
      ) {
        issues.push({
          code: "duplicate_view_module_binding",
          path: `${path}.viewId`,
          message: `view ${module.viewId} has more than one page module`,
        });
      }
      if (!localViewIds.has(module.viewId)) {
        issues.push({
          code: "orphan_view_module",
          path: `${path}.viewId`,
          message: "must bind a view definition from the same App contribution",
        });
      }
      if (typeof module.load !== "function") {
        issues.push({
          code: "invalid_view_module_loader",
          path: `${path}.load`,
          message: "must be a lazy module loader function",
        });
      }
      localModuleIds.add(module.moduleId);
      localModuleViewIds.add(module.viewId);
    });

    return issues;
  }
}

export const createContributionRegistry = (): ContributionRegistry =>
  new ContributionRegistry();
