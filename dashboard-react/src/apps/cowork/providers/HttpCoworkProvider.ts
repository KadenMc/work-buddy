import type {
  AppInvalidation,
  DashboardIntent,
  IntentResult,
  ReconcileResult,
  ViewLoadRequest,
  ViewSnapshot,
  WidgetLoadRequest,
  WidgetSnapshot,
  WidgetTypeId,
} from "../../../dashboard/contributions/contracts";
import type { ViewProvider } from "../../../dashboard/providers/ViewProvider";
import { COWORK_APP_ID, COWORK_VIEW_ID } from "../bindings";
import {
  COWORK_INTENTS,
  type CoworkActiveSession,
  type CoworkApiError,
  type CoworkCatalogState,
  type CoworkDocumentOpenIntentPayload,
  type CoworkDocumentSummary,
  type CoworkFolderCandidate,
  type CoworkFolderSelectIntentPayload,
  type CoworkFolderSelection,
  type CoworkRouteTarget,
  type CoworkScratchCloseIntentPayload,
  type CoworkScratchOpenIntentPayload,
  type CoworkScratchTouchIntentPayload,
  type CoworkViewModel,
  type CoworkWorkspaceInput,
} from "../contracts";
import {
  CoworkScratchRegistry,
  type CoworkScratchTransportFactory,
} from "../scratch/registry";
import {
  CoworkHttpClient,
  type CoworkInspectionResult,
} from "./CoworkHttpClient";
import { asCoworkApiError, CoworkHttpError } from "./errors";
import {
  coworkSessionDurability,
  registeredSessionDurabilityKey,
  scratchSessionDurabilityKey,
} from "../session/CoworkSessionDurability";
import {
  coworkStoreUrlId,
  resolveCoworkRouteStoreId,
} from "./storeRouteIdentity";

export interface CoworkLocationAdapter {
  getSearch(): string;
  pushSearch(search: string): void;
  replaceSearch(search: string): void;
  subscribe(listener: (search: string) => void): () => void;
}

export interface HttpCoworkProviderOptions {
  readonly location: CoworkLocationAdapter;
  readonly storage: Storage;
  readonly client?: CoworkHttpClient;
  readonly scratchTransportFactory?: CoworkScratchTransportFactory;
}

const emptyCatalog = (status: CoworkCatalogState["status"] = "empty"): CoworkCatalogState => ({
  status,
  documents: [],
  refreshedAt: null,
  error: null,
});

const readyDocument = (document: CoworkDocumentSummary): boolean =>
  (document.initializationState ?? "ready") === "ready" &&
  document.lifecycle !== "retired" &&
  document.permissions?.open !== false;

const idempotencyKey = (): string =>
  globalThis.crypto?.randomUUID?.() ??
  `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;

const payloadRecord = (intent: DashboardIntent): Record<string, unknown> =>
  typeof intent.payload === "object" && intent.payload !== null
    ? (intent.payload as Record<string, unknown>)
    : {};

const folderActionError = (error: unknown): CoworkApiError => {
  const apiError = asCoworkApiError(error);
  const messages: Readonly<Record<string, string>> = {
    folder_chooser_unavailable: "Choosing a folder isn’t available here.",
    folder_chooser_busy: "The folder picker is already open.",
    folder_chooser_failed: "The folder picker couldn’t be opened.",
    folder_chooser_timeout: "The folder picker took too long. Try again.",
    selection_expired: "The folder selection expired. Open the folder again.",
    folder_changed: "The folder changed while it was opening. Try again.",
    descendant_scan_incomplete: "Co-work couldn’t finish opening that folder. Try again.",
    folder_unreachable: "Co-work couldn’t open that folder.",
    network_error: "Co-work couldn’t finish opening that folder. Try again.",
    request_failed: "Co-work couldn’t finish opening that folder. Try again.",
  };
  return {
    ...apiError,
    message: messages[apiError.code] ?? apiError.message,
  };
};

const routeFromSearch = (
  search: string,
  scratchRegistry: CoworkScratchRegistry,
): CoworkRouteTarget => {
  const params = new URLSearchParams(search);
  const storeId = params.get("store_id");
  const documentId = params.get("document_id");
  const scratchId = params.get("scratch_id");
  if (storeId !== null && documentId !== null) {
    return { kind: "registered", storeId, documentId };
  }
  if (scratchId !== null) {
    const scratch = scratchRegistry.find(scratchId);
    return {
      kind: "scratch",
      scratchId,
      title: scratch?.title ?? "Document not found",
    };
  }
  if (storeId !== null) return { kind: "launcher", storeId };
  return { kind: "launcher", storeId: null };
};

const activeDocument = (session: CoworkActiveSession): CoworkDocumentSummary | null =>
  session.kind === "registered" ? session.document : null;

const sessionDurabilityKey = (session: CoworkActiveSession): string | null => {
  if (session.kind === "registered") {
    return registeredSessionDurabilityKey(session.storeId, session.document.documentId);
  }
  if (session.kind === "scratch") return scratchSessionDurabilityKey(session.scratchId);
  return null;
};

const routeDurabilityKey = (route: CoworkRouteTarget): string | null => {
  if (route.kind === "registered") {
    return registeredSessionDurabilityKey(route.storeId, route.documentId);
  }
  if (route.kind === "scratch") return scratchSessionDurabilityKey(route.scratchId);
  return null;
};

/** HTTP-backed coarse lifecycle provider. Opaque setup tokens never leave this class. */
export class HttpCoworkProvider implements ViewProvider {
  readonly appId = COWORK_APP_ID;
  readonly #location: CoworkLocationAdapter;
  readonly #client: CoworkHttpClient;
  readonly #scratches: CoworkScratchRegistry;
  readonly #listeners = new Set<(invalidation: AppInvalidation) => void>();
  #revision = 1;
  #requestEpoch = 0;
  #catalogEpoch = 0;
  #boot?: Promise<void>;
  #inspectionToken: string | null = null;
  #inspectionEpoch = 0;
  #folderActivationEpoch = 0;
  #ignoredLocationSearch: string | null = null;
  #folderMutationKey: {
    readonly inspectionToken: string;
    readonly operation: "initialize";
    readonly key: string;
  } | null = null;
  #continuationToken: string | null = null;
  #pendingCandidate: CoworkFolderCandidate | null = null;
  #selectionBeforeInspection: CoworkFolderSelection = { kind: "none" };
  #lastInspectionInput: { readonly folderPath?: string; readonly selectionToken?: string } = {};
  #model: CoworkViewModel;

  constructor(options: HttpCoworkProviderOptions) {
    this.#location = options.location;
    this.#client = options.client ?? new CoworkHttpClient();
    this.#scratches = new CoworkScratchRegistry(
      options.storage,
      options.scratchTransportFactory,
    );
    const routeTarget = routeFromSearch(options.location.getSearch(), this.#scratches);
    const requestedStoreId =
      routeTarget.kind === "scratch" ? null : routeTarget.storeId;
    this.#model = {
      folders: [],
      folderChooser: {
        available: true,
        kind: "host",
        importAvailable: true,
        locationAvailable: true,
      },
      folderSelection: { kind: "none" },
      // A URL carries a request, not proof that a Folder is initialized and reachable.
      activeFolderStoreId: null,
      catalog:
        routeTarget.kind === "registered" || requestedStoreId !== null
          ? emptyCatalog("loading")
          : emptyCatalog(),
      scratches: this.#scratches.list(),
      routeTarget,
      activeSession: { kind: "none" },
      openingTarget: null,
      navigationError: null,
      readOnly: false,
      document: null,
    };
    options.location.subscribe((search) => {
      const ignoredSearch = this.#ignoredLocationSearch;
      this.#ignoredLocationSearch = null;
      if (search === ignoredSearch) {
        return;
      }
      void this.#followLocation(search);
    });
  }

  subscribeInvalidations(listener: (invalidation: AppInvalidation) => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  async loadView(
    _viewId = COWORK_VIEW_ID,
    _request?: ViewLoadRequest,
  ): Promise<ViewSnapshot<CoworkViewModel>> {
    await this.#ensureBooted();
    return this.#viewSnapshot();
  }

  async loadWidget(
    widgetTypeId: WidgetTypeId,
    request: WidgetLoadRequest,
  ): Promise<WidgetSnapshot<CoworkWorkspaceInput>> {
    await this.#ensureBooted();
    return {
      widgetTypeId,
      instanceId: request.instanceId,
      revision: this.#revision,
      observedAt: new Date().toISOString(),
      status: this.#model.readOnly ? "read-only" : "ready",
      quality: {
        kind: this.#model.navigationError === null ? "complete" : "partial",
        ...(this.#model.navigationError === null
          ? {}
          : { message: this.#model.navigationError.message }),
      },
      input: { ...this.#model, sessionQuality: "complete" },
    };
  }

  async dispatch(intent: DashboardIntent): Promise<IntentResult> {
    try {
      switch (intent.intent_type) {
        case COWORK_INTENTS.folderSelect: {
          const input = payloadRecord(intent) as unknown as CoworkFolderSelectIntentPayload;
          if (input.action === "open" || input.action === "initialize") {
            await this.#withDurableSessionLeave(null, () => this.#handleFolderIntent(input));
          } else {
            await this.#handleFolderIntent(input);
          }
          break;
        }
        case COWORK_INTENTS.folderClose: {
          const targetKey =
            this.#model.activeSession.kind === "scratch"
              ? scratchSessionDurabilityKey(this.#model.activeSession.scratchId)
              : null;
          await this.#withDurableSessionLeave(targetKey, () => this.#closeFolder());
          break;
        }
        case COWORK_INTENTS.catalogRefresh:
          if (this.#model.activeFolderStoreId !== null) {
            await this.#loadCatalog(this.#model.activeFolderStoreId);
            this.#touch("catalog-refreshed");
          }
          break;
        case COWORK_INTENTS.documentOpen: {
          const input = payloadRecord(intent) as unknown as CoworkDocumentOpenIntentPayload;
          await this.#withDurableSessionLeave(
            registeredSessionDurabilityKey(input.storeId, input.documentId),
            () => this.#openDocument(input, true),
          );
          break;
        }
        case COWORK_INTENTS.documentReload: {
          const input = payloadRecord(intent) as unknown as CoworkDocumentOpenIntentPayload;
          await this.#reloadActiveDocument(input);
          break;
        }
        case COWORK_INTENTS.documentClose:
          await this.#withDurableSessionLeave(null, () => this.#closeDocument());
          break;
        case COWORK_INTENTS.scratchOpen: {
          const input = payloadRecord(intent) as unknown as CoworkScratchOpenIntentPayload;
          await this.#withDurableSessionLeave(
            input.scratchId === undefined ? null : scratchSessionDurabilityKey(input.scratchId),
            () => this.#openScratch(input),
          );
          break;
        }
        case COWORK_INTENTS.scratchClose: {
          const input = payloadRecord(intent) as unknown as CoworkScratchCloseIntentPayload;
          if (input.retire === true && this.#model.activeSession.kind !== "scratch") {
            await this.#closeScratch(input);
          } else {
            await this.#withDurableSessionLeave(null, () => this.#closeScratch(input));
          }
          break;
        }
        case COWORK_INTENTS.scratchTouch: {
          const input = payloadRecord(intent) as unknown as CoworkScratchTouchIntentPayload;
          this.#touchScratch(input);
          break;
        }
        default:
          return {
            intent_id: intent.intent_id,
            status: "rejected",
            message: `Unsupported Co-work action: ${intent.intent_type}`,
          };
      }
      return {
        intent_id: intent.intent_id,
        status: "accepted",
        revision: this.#revision,
      };
    } catch (error) {
      const apiError =
        intent.intent_type === COWORK_INTENTS.folderSelect ||
        intent.intent_type === COWORK_INTENTS.folderClose
          ? folderActionError(error)
          : asCoworkApiError(error);
      this.#patch({ navigationError: apiError }, `intent-failed:${intent.intent_type}`);
      return {
        intent_id: intent.intent_id,
        status: apiError.code.includes("conflict") ? "conflict" : "rejected",
        revision: this.#revision,
        message: apiError.message,
        ...(apiError.field === undefined
          ? {}
          : { fieldErrors: { [apiError.field]: apiError.message } }),
      };
    }
  }

  async reconcile(_invalidation: AppInvalidation): Promise<ReconcileResult> {
    return { changed: true, revision: this.#revision };
  }

  async #withDurableSessionLeave<Value>(
    targetKey: string | null,
    operation: () => Promise<Value> | Value,
  ): Promise<Value> {
    const sourceKey = sessionDurabilityKey(this.#model.activeSession);
    if (sourceKey === null || sourceKey === targetKey) return operation();
    const preparing = coworkSessionDurability.prepareToLeave(sourceKey);
    if (preparing === null) return operation();
    const lease = await preparing;
    try {
      const result = await operation();
      // A superseded async open can resolve without changing the session. In that case the
      // current editor remains authoritative and must be resumed, not abandoned.
      if (sessionDurabilityKey(this.#model.activeSession) === sourceKey) lease.cancel();
      else lease.commit();
      return result;
    } catch (error) {
      lease.cancel();
      throw error;
    }
  }

  async #reloadActiveDocument(input: CoworkDocumentOpenIntentPayload): Promise<void> {
    const sourceKey = sessionDurabilityKey(this.#model.activeSession);
    const expectedKey = registeredSessionDurabilityKey(input.storeId, input.documentId);
    if (sourceKey !== expectedKey) {
      throw new CoworkHttpError({
        code: "document_reload_unavailable",
        message: "Open this document before retrying its reload.",
        retryable: true,
      });
    }

    const preparing = coworkSessionDurability.prepareToLeave(expectedKey);
    const lease = preparing === null ? null : await preparing;
    // A reload deliberately replaces a session with the same durability key, so the
    // ordinary navigation helper cannot distinguish its old and new editor controllers.
    // Stage every network read off-model, then commit the lease and model together.
    const requestEpoch = ++this.#requestEpoch;
    const catalogEpoch = ++this.#catalogEpoch;
    const stillCurrent = (): boolean =>
      requestEpoch === this.#requestEpoch &&
      catalogEpoch === this.#catalogEpoch &&
      this.#model.activeFolderStoreId === input.storeId &&
      sessionDurabilityKey(this.#model.activeSession) === expectedKey;
    try {
      if (!stillCurrent()) {
        lease?.cancel();
        return;
      }

      const documents = await this.#client.listDocuments(input.storeId);
      if (!stillCurrent()) {
        lease?.cancel();
        return;
      }
      const document = documents.find(
        (entry) => entry.documentId === input.documentId,
      );
      if (document === undefined) {
        throw new CoworkHttpError({
          code: "document_reload_missing",
          message: "The committed document replacement is not in this folder yet.",
          retryable: true,
        });
      }
      if (!readyDocument(document)) {
        throw new CoworkHttpError({
          code: document.initializationState ?? "document_unavailable",
          message:
            document.disabledReason ??
            "This document needs attention before Co-work can reopen it.",
          retryable: document.permissions?.repair === true,
        });
      }
      await this.#client.preflightDocument(input.storeId, input.documentId);
      if (!stillCurrent()) {
        lease?.cancel();
        return;
      }

      const target: CoworkRouteTarget = {
        kind: "registered",
        storeId: input.storeId,
        documentId: input.documentId,
      };
      const catalogStatus: CoworkCatalogState["status"] =
        this.#model.readOnly ||
        this.#model.folders.find((folder) => folder.storeId === input.storeId)
          ?.permissions.create === false
          ? "read-only"
          : documents.length === 0
            ? "empty"
            : "ready";
      lease?.commit();
      this.#model = {
        ...this.#model,
        catalog: {
          status: catalogStatus,
          documents,
          refreshedAt: new Date().toISOString(),
          error: null,
        },
        routeTarget: target,
        openingTarget: null,
        navigationError: null,
        activeSession: {
          kind: "registered",
          storeId: input.storeId,
          document,
        },
        document,
      };
    } catch (error) {
      lease?.cancel();
      throw error;
    }
    this.#touch("document-reloaded");
  }

  async #ensureBooted(): Promise<void> {
    if (this.#boot !== undefined) return this.#boot;
    this.#boot = this.#bootstrap();
    return this.#boot;
  }

  async #bootstrap(): Promise<void> {
    const requestedRoute = routeFromSearch(this.#location.getSearch(), this.#scratches);
    try {
      const [folders] = await Promise.all([
        this.#client.listFolders(true),
        this.#scratches.discoverPreviousEditorScratch(),
      ]);
      this.#model = {
        ...this.#model,
        folders: folders.folders,
        folderChooser: folders.chooser,
        scratches: this.#scratches.list(),
        readOnly: folders.readOnly,
      };
      const route = resolveCoworkRouteStoreId(requestedRoute, folders.folders);
      await this.#resolveRoute(route, false);
      this.#replaceWithCanonicalStoreUrl(route);
    } catch (error) {
      const apiError = asCoworkApiError(error);
      this.#model = {
        ...this.#model,
        routeTarget: requestedRoute,
        navigationError: apiError,
        catalog: { ...emptyCatalog("error"), error: apiError },
      };
    }
  }

  async #followLocation(search: string): Promise<void> {
    await this.#ensureBooted();
    const route = resolveCoworkRouteStoreId(
      routeFromSearch(search, this.#scratches),
      this.#model.folders,
    );
    const previousSearch = this.#activeSessionSearch();
    try {
      await this.#withDurableSessionLeave(routeDurabilityKey(route), () =>
        this.#resolveRoute(route, true),
      );
      this.#replaceWithCanonicalStoreUrl(route);
    } catch (error) {
      // Browser Back/Forward changes the address before the provider can run its barrier.
      // Restore the still-open session when device-local persistence fails.
      this.#location.replaceSearch(previousSearch);
      const apiError = asCoworkApiError(error);
      this.#patch({ navigationError: apiError }, "location-durability-failed");
    }
  }

  #activeSessionSearch(): string {
    const session = this.#model.activeSession;
    if (session.kind === "registered") {
      return this.#storeSearch(session.storeId, session.document.documentId);
    }
    if (session.kind === "scratch") {
      return `?scratch_id=${encodeURIComponent(session.scratchId)}`;
    }
    const storeId = this.#model.activeFolderStoreId;
    return storeId === null ? "?mode=launcher" : this.#storeSearch(storeId);
  }

  #storeSearch(storeId: string, documentId?: string): string {
    const urlStoreId = coworkStoreUrlId(storeId, this.#model.folders);
    const documentSearch =
      documentId === undefined ? "" : `&document_id=${encodeURIComponent(documentId)}`;
    return `?store_id=${encodeURIComponent(urlStoreId)}${documentSearch}`;
  }

  #replaceWithCanonicalStoreUrl(route: CoworkRouteTarget): void {
    if (route.kind === "scratch" || route.kind === "unavailable" || route.storeId === null) {
      return;
    }

    const hasResolvedRoute =
      route.kind === "registered"
        ? this.#model.activeSession.kind === "registered" &&
          this.#model.activeSession.storeId === route.storeId &&
          this.#model.activeSession.document.documentId === route.documentId
        : this.#model.activeFolderStoreId === route.storeId;
    if (!hasResolvedRoute) return;

    const params = new URLSearchParams(this.#location.getSearch());
    const urlStoreId = coworkStoreUrlId(route.storeId, this.#model.folders);
    if (params.get("store_id") === urlStoreId) return;

    params.set("store_id", urlStoreId);
    const nextSearch = `?${params.toString()}`;
    this.#ignoredLocationSearch = nextSearch;
    this.#location.replaceSearch(nextSearch);
  }

  async #resolveRoute(route: CoworkRouteTarget, notify: boolean): Promise<void> {
    const epoch = ++this.#requestEpoch;
    this.#model = {
      ...this.#model,
      routeTarget: route,
      openingTarget: route.kind === "launcher" ? null : route,
      navigationError: null,
    };

    if (route.kind === "launcher") {
      if (route.storeId === null) {
        this.#model = {
          ...this.#model,
          activeFolderStoreId: null,
          folderSelection: { kind: "none" },
          activeSession: { kind: "none" },
          openingTarget: null,
          catalog: emptyCatalog(),
          document: null,
        };
      } else {
        const activated = await this.#activateFolder(
          route.storeId,
          () => epoch === this.#requestEpoch,
        );
        if (!activated || epoch !== this.#requestEpoch) return;
        this.#model = {
          ...this.#model,
          activeSession: { kind: "none" },
          openingTarget: null,
          document: null,
        };
      }
      if (notify) this.#touch("location:launcher");
      return;
    }

    if (route.kind === "scratch") {
      const scratch = this.#scratches.find(route.scratchId);
      if (scratch === undefined) {
        const error: CoworkApiError = {
          code: "scratch_not_found",
          message: "This document was not found on this device.",
          retryable: false,
        };
        this.#model = {
          ...this.#model,
          routeTarget: {
            kind: "launcher",
            storeId: this.#model.activeFolderStoreId,
          },
          openingTarget: null,
          navigationError: error,
          activeSession: { kind: "none" },
          document: null,
        };
      } else {
        this.#model = {
          ...this.#model,
          routeTarget: { ...route, title: scratch.title },
          openingTarget: null,
          activeSession: {
            kind: "scratch",
            scratchId: scratch.scratchId,
            title: scratch.title,
          },
          document: null,
        };
      }
      if (notify) this.#touch("location:scratch");
      return;
    }

    if (route.kind === "unavailable") {
      if (notify) this.#touch("location:unavailable");
      return;
    }

    await this.#openDocument(
      { storeId: route.storeId, documentId: route.documentId },
      false,
      epoch,
    );
    if (notify && epoch === this.#requestEpoch) this.#touch("location:document");
  }

  async #activateFolder(
    storeId: string,
    isCurrent: () => boolean = () => true,
  ): Promise<boolean> {
    let folder = this.#model.folders.find((entry) => entry.storeId === storeId);
    if (folder === undefined) {
      const folders = await this.#client.listFolders(true);
      if (!isCurrent()) return false;
      folder = folders.folders.find((entry) => entry.storeId === storeId);
      this.#model = {
        ...this.#model,
        folders: folders.folders,
        folderChooser: folders.chooser,
        readOnly: folders.readOnly,
      };
    }
    if (!isCurrent()) return false;
    if (folder === undefined) {
      const error: CoworkApiError = {
        code: "folder_not_found",
        message: "This folder is no longer available.",
        retryable: true,
      };
      const hasPreviousFolder = this.#model.activeFolderStoreId !== null;
      this.#model = {
        ...this.#model,
        folderSelection: {
          kind: "unavailable",
          candidate: null,
          reasonCode: "folder_not_found",
          retryable: true,
        },
        ...(hasPreviousFolder
          ? {}
          : {
              activeFolderStoreId: null,
              catalog: { ...emptyCatalog("unreachable"), error },
            }),
        navigationError: error,
      };
      throw new CoworkHttpError(error);
    }
    this.#model = {
      ...this.#model,
      activeFolderStoreId: folder.storeId,
      folderSelection: { kind: "initialized", folder },
      navigationError: null,
    };
    await this.#loadCatalog(folder.storeId);
    return isCurrent();
  }

  async #loadCatalog(storeId: string): Promise<void> {
    const catalogEpoch = ++this.#catalogEpoch;
    this.#model = {
      ...this.#model,
      catalog: { ...this.#model.catalog, status: "loading", error: null },
    };
    try {
      const documents = await this.#client.listDocuments(storeId);
      if (
        catalogEpoch !== this.#catalogEpoch ||
        this.#model.activeFolderStoreId !== storeId
      ) {
        return;
      }
      const activeDocumentId =
        this.#model.activeSession.kind === "registered" &&
        this.#model.activeSession.storeId === storeId
          ? this.#model.activeSession.document.documentId
          : null;
      const activeDocument =
        activeDocumentId === null
          ? undefined
          : documents.find((document) => document.documentId === activeDocumentId);
      const catalog: CoworkCatalogState = {
        status:
          this.#model.readOnly ||
          this.#model.folders.find((folder) => folder.storeId === storeId)?.permissions.create === false
            ? "read-only"
            : documents.length === 0
              ? "empty"
              : "ready",
        documents,
        refreshedAt: new Date().toISOString(),
        error: null,
      };
      if (activeDocumentId !== null && activeDocument === undefined) {
        await this.#withDurableSessionLeave(null, () => {
          // The catalog request may have been superseded while IndexedDB was making the
          // old editor safe to unmount. Commit the unavailable state only if this exact
          // document is still active; otherwise the durability lease resumes its owner.
          if (
            catalogEpoch !== this.#catalogEpoch ||
            this.#model.activeFolderStoreId !== storeId ||
            this.#model.activeSession.kind !== "registered" ||
            this.#model.activeSession.storeId !== storeId ||
            this.#model.activeSession.document.documentId !== activeDocumentId
          ) {
            return;
          }
          const unavailable: CoworkApiError = {
            code: "document_no_longer_available",
            message:
              "This document is no longer active in Co-work. Edits captured before it changed are saved on this device.",
            retryable: false,
            details: { documentId: activeDocumentId },
          };
          this.#model = {
            ...this.#model,
            catalog,
            routeTarget: {
              kind: "unavailable",
              storeId,
              documentId: activeDocumentId,
              reason: unavailable.code,
            },
            openingTarget: null,
            navigationError: unavailable,
            activeSession: { kind: "none" },
            document: null,
          };
        });
        return;
      }
      this.#model = {
        ...this.#model,
        catalog,
        ...(activeDocument === undefined
          ? {}
          : {
              activeSession: {
                kind: "registered" as const,
                storeId,
                document: activeDocument,
              },
              document: activeDocument,
            }),
      };
    } catch (error) {
      if (
        catalogEpoch !== this.#catalogEpoch ||
        this.#model.activeFolderStoreId !== storeId
      ) {
        return;
      }
      const apiError = asCoworkApiError(error);
      this.#model = {
        ...this.#model,
        catalog: { ...emptyCatalog("error"), error: apiError },
      };
    }
  }

  async #openDocument(
    input: CoworkDocumentOpenIntentPayload,
    navigate: boolean,
    existingEpoch?: number,
  ): Promise<void> {
    const previousTarget = this.#model.routeTarget;
    const previousFolderSelection = this.#model.folderSelection;
    const hadActiveSession = this.#model.activeSession.kind !== "none";
    const target: CoworkRouteTarget = {
      kind: "registered",
      storeId: input.storeId,
      documentId: input.documentId,
    };
    const epoch = existingEpoch ?? ++this.#requestEpoch;
    this.#model = {
      ...this.#model,
      routeTarget: target,
      openingTarget: target,
      navigationError: null,
    };
    if (this.#model.activeFolderStoreId !== input.storeId) {
      try {
        const activated = await this.#activateFolder(
          input.storeId,
          () => epoch === this.#requestEpoch,
        );
        if (!activated || epoch !== this.#requestEpoch) return;
      } catch (error) {
        if (epoch !== this.#requestEpoch) return;
        const apiError = asCoworkApiError(error);
        this.#model = {
          ...this.#model,
          ...(hadActiveSession
            ? { folderSelection: previousFolderSelection }
            : {}),
          openingTarget: null,
          navigationError: apiError,
          routeTarget:
            navigate || hadActiveSession
              ? previousTarget
              : {
                  kind: "unavailable",
                  storeId: input.storeId,
                  documentId: input.documentId,
                  reason: apiError.code,
                },
        };
        if (navigate || hadActiveSession) throw new CoworkHttpError(apiError);
        return;
      }
    }
    let document = this.#model.catalog.documents.find(
      (entry) => entry.documentId === input.documentId,
    );
    if (document === undefined) {
      try {
        document = await this.#client.readDocument(input.storeId, input.documentId);
      } catch (error) {
        if (epoch !== this.#requestEpoch) return;
        const apiError = asCoworkApiError(error);
        this.#model = {
          ...this.#model,
          openingTarget: null,
          navigationError: apiError,
          routeTarget: navigate
            ? previousTarget
            : {
                kind: "unavailable",
                storeId: input.storeId,
                documentId: input.documentId,
                reason: apiError.code,
              },
        };
        if (navigate) throw new CoworkHttpError(apiError);
        return;
      }
    }
    if (epoch !== this.#requestEpoch) return;
    if (!readyDocument(document)) {
      const error: CoworkApiError = {
        code: document.initializationState ?? "document_unavailable",
        message:
          document.disabledReason ??
          "This document needs attention before Co-work can open it.",
        retryable: document.permissions?.repair === true,
      };
      this.#model = {
        ...this.#model,
        openingTarget: null,
        navigationError: error,
        routeTarget: navigate
          ? previousTarget
          : {
              kind: "unavailable",
              storeId: input.storeId,
              documentId: input.documentId,
              reason: error.code,
            },
      };
      if (navigate) throw new CoworkHttpError(error);
      return;
    }
    try {
      await this.#client.preflightDocument(input.storeId, input.documentId);
    } catch (error) {
      if (epoch !== this.#requestEpoch) return;
      const apiError = asCoworkApiError(error);
      if (apiError.code === "semantic_corrupt") {
        const semanticCorrupt: CoworkDocumentSummary = {
          ...document,
          initializationState: "semantic_corrupt",
          disabledReason: apiError.message,
          permissions: {
            open: false,
            edit: false,
            materialize: false,
            repair: true,
            retire: document.permissions?.retire ?? true,
          },
        };
        this.#model = {
          ...this.#model,
          catalog: {
            ...this.#model.catalog,
            documents: this.#model.catalog.documents.map((entry) =>
              entry.documentId === semanticCorrupt.documentId
                ? semanticCorrupt
                : entry,
            ),
          },
        };
      }
      this.#model = {
        ...this.#model,
        openingTarget: null,
        navigationError: apiError,
        routeTarget: navigate
          ? previousTarget
          : {
              kind: "unavailable",
              storeId: input.storeId,
              documentId: input.documentId,
              reason: apiError.code,
            },
      };
      if (navigate) throw new CoworkHttpError(apiError);
      return;
    }
    if (epoch !== this.#requestEpoch) return;
    this.#model = {
      ...this.#model,
      routeTarget: target,
      openingTarget: null,
      navigationError: null,
      activeSession: { kind: "registered", storeId: input.storeId, document },
      document,
    };
    if (navigate) {
      this.#location.pushSearch(this.#storeSearch(input.storeId, input.documentId));
    }
    this.#touch("document-opened");
  }

  async #closeDocument(): Promise<void> {
    const storeId = this.#model.activeFolderStoreId;
    this.#model = {
      ...this.#model,
      routeTarget: { kind: "launcher", storeId },
      activeSession: { kind: "none" },
      openingTarget: null,
      navigationError: null,
      document: null,
    };
    this.#location.pushSearch(
      storeId === null ? "?mode=launcher" : this.#storeSearch(storeId),
    );
    this.#touch("document-closed");
  }

  #closeFolder(): void {
    this.#requestEpoch += 1;
    this.#catalogEpoch += 1;
    this.#inspectionEpoch += 1;
    this.#folderActivationEpoch += 1;
    this.#inspectionToken = null;
    this.#folderMutationKey = null;
    this.#continuationToken = null;
    this.#pendingCandidate = null;
    this.#selectionBeforeInspection = { kind: "none" };
    this.#lastInspectionInput = {};

    const session = this.#model.activeSession;
    const keepScratch = session.kind === "scratch";
    const routeTarget: CoworkRouteTarget = keepScratch
      ? {
          kind: "scratch",
          scratchId: session.scratchId,
          title: session.title,
        }
      : { kind: "launcher", storeId: null };

    this.#model = {
      ...this.#model,
      activeFolderStoreId: null,
      folderSelection: { kind: "none" },
      catalog: emptyCatalog(),
      routeTarget,
      activeSession: keepScratch ? session : { kind: "none" },
      openingTarget: null,
      navigationError: null,
      document: null,
    };
    this.#location.pushSearch(
      keepScratch
        ? `?scratch_id=${encodeURIComponent(session.scratchId)}`
        : "?mode=launcher",
    );
    this.#touch("folder-closed");
  }

  #openScratch(input: CoworkScratchOpenIntentPayload): void {
    const scratch =
      input.scratchId === undefined
        ? this.#scratches.create(input.title)
        : this.#scratches.find(input.scratchId);
    if (scratch === undefined) {
      throw new Error("This document was not found on this device.");
    }
    this.#model = {
      ...this.#model,
      scratches: this.#scratches.list(),
      routeTarget: {
        kind: "scratch",
        scratchId: scratch.scratchId,
        title: scratch.title,
      },
      activeSession: {
        kind: "scratch",
        scratchId: scratch.scratchId,
        title: scratch.title,
      },
      openingTarget: null,
      navigationError: null,
      document: null,
    };
    this.#location.pushSearch(`?scratch_id=${encodeURIComponent(scratch.scratchId)}`);
    this.#touch("scratch-opened");
  }

  #touchScratch(input: CoworkScratchTouchIntentPayload): void {
    if (
      this.#model.activeSession.kind !== "scratch" ||
      this.#model.activeSession.scratchId !== input.scratchId
    ) {
      throw new Error("That document is not open on this device.");
    }
    this.#scratches.touch(input.scratchId);
    this.#model = {
      ...this.#model,
      scratches: this.#scratches.list(),
    };
    this.#touch("scratch-edited");
  }

  async #closeScratch(input: CoworkScratchCloseIntentPayload): Promise<void> {
    if (input.retire === true) {
      if (input.scratchId === undefined || input.scratchId.length === 0) {
        throw new Error("Choose the local document to remove.");
      }
      if (this.#model.activeSession.kind === "scratch") {
        if (this.#model.activeSession.scratchId !== input.scratchId) {
          throw new Error("That document is not open.");
        }
        await this.#scratches.discard(input.scratchId);
        const storeId = this.#model.activeFolderStoreId;
        this.#model = {
          ...this.#model,
          scratches: this.#scratches.list(),
          routeTarget: { kind: "launcher", storeId },
          activeSession: { kind: "none" },
          openingTarget: null,
          navigationError: null,
          document: null,
        };
        this.#location.pushSearch(
          storeId === null ? "?mode=launcher" : this.#storeSearch(storeId),
        );
        this.#touch("local-document-discarded");
        return;
      }
      if (this.#model.activeSession.kind !== "registered") {
        throw new Error(
          "This document remains on this device until its saved copy is open.",
        );
      }
      await this.#scratches.discard(input.scratchId);
      this.#model = {
        ...this.#model,
        scratches: this.#scratches.list(),
      };
      this.#touch("scratch-retired-after-promotion");
      return;
    }
    this.#model = {
      ...this.#model,
      routeTarget: { kind: "launcher", storeId: null },
      activeSession: { kind: "none" },
      openingTarget: null,
      navigationError: null,
      document: null,
    };
    this.#location.pushSearch("?mode=launcher");
    this.#touch("scratch-closed");
  }

  async #handleFolderIntent(input: CoworkFolderSelectIntentPayload): Promise<void> {
    if (input.action !== "open") {
      this.#folderActivationEpoch += 1;
    }
    if (input.action === "cancel") {
      this.#inspectionEpoch += 1;
      this.#inspectionToken = null;
      this.#folderMutationKey = null;
      this.#continuationToken = null;
      this.#pendingCandidate = null;
      this.#patch(
        { folderSelection: this.#selectionBeforeInspection, navigationError: null },
        "folder-selection-cancelled",
      );
      return;
    }
    if (input.action === "open") {
      if (input.storeId === undefined) throw new Error("Choose a folder to open.");
      if (
        this.#model.folderSelection.kind === "initialized" ||
        this.#model.folderSelection.kind === "none"
      ) {
        this.#selectionBeforeInspection = this.#model.folderSelection;
      }
      const activationEpoch = ++this.#folderActivationEpoch;
      let activated = false;
      try {
        activated = await this.#activateFolder(
          input.storeId,
          () => activationEpoch === this.#folderActivationEpoch,
        );
      } catch (error) {
        if (activationEpoch !== this.#folderActivationEpoch) return;
        throw error;
      }
      if (!activated || activationEpoch !== this.#folderActivationEpoch) return;
      this.#model = {
        ...this.#model,
        routeTarget: { kind: "launcher", storeId: input.storeId },
        activeSession: { kind: "none" },
        document: null,
      };
      this.#location.pushSearch(this.#storeSearch(input.storeId));
      this.#touch("folder-opened");
      return;
    }
    if (input.action === "choose") {
      const inspectionEpoch = ++this.#inspectionEpoch;
      if (
        this.#model.folderSelection.kind === "initialized" ||
        this.#model.folderSelection.kind === "none"
      ) {
        this.#selectionBeforeInspection = this.#model.folderSelection;
      }
      this.#inspectionToken = null;
      this.#continuationToken = null;
      this.#folderMutationKey = null;
      this.#patch(
        { folderSelection: { kind: "choosing" }, navigationError: null },
        "folder-choosing",
      );
      let chosen: Awaited<ReturnType<CoworkHttpClient["chooseFolder"]>>;
      try {
        chosen = await this.#client.chooseFolder();
      } catch (error) {
        this.#restoreTransientFolderSelection(inspectionEpoch);
        throw error;
      }
      if (inspectionEpoch !== this.#inspectionEpoch) return;
      if (chosen.cancelled) {
        this.#patch(
          {
            folderSelection: this.#selectionBeforeInspection,
            navigationError: null,
          },
          "folder-choose-cancelled",
        );
        return;
      }
      this.#pendingCandidate = chosen;
      this.#lastInspectionInput =
        chosen.selectionToken === null
          ? { folderPath: chosen.folderPath }
          : { selectionToken: chosen.selectionToken };
      await this.#inspectWithFailureRecovery(this.#lastInspectionInput, inspectionEpoch);
      return;
    }
    if (input.action === "inspect") {
      const inspectionEpoch = ++this.#inspectionEpoch;
      if (
        this.#model.folderSelection.kind === "initialized" ||
        this.#model.folderSelection.kind === "none"
      ) {
        this.#selectionBeforeInspection = this.#model.folderSelection;
      }
      if (input.folderPath === undefined || input.folderPath.trim().length === 0) {
        throw new Error("Choose a folder to open.");
      }
      this.#pendingCandidate = {
        folderName: (() => {
          const parts = input.folderPath.replace(/[\\/]+$/, "").split(/[\\/]/);
          return parts[parts.length - 1] ?? input.folderPath;
        })(),
        folderPath: input.folderPath,
      };
      this.#lastInspectionInput = { folderPath: input.folderPath };
      await this.#inspectWithFailureRecovery(this.#lastInspectionInput, inspectionEpoch);
      return;
    }
    if (input.action === "continue") {
      if (this.#continuationToken === null) throw new Error("The folder check expired. Try again.");
      const inspectionEpoch = ++this.#inspectionEpoch;
      await this.#inspectWithFailureRecovery(
        { continuationToken: this.#continuationToken },
        inspectionEpoch,
      );
      return;
    }
    if (input.action === "retry") {
      const inspectionEpoch = ++this.#inspectionEpoch;
      await this.#inspectWithFailureRecovery(
        this.#continuationToken === null
          ? this.#lastInspectionInput
          : { continuationToken: this.#continuationToken },
        inspectionEpoch,
      );
      return;
    }
    if (input.action === "initialize") {
      if (this.#model.readOnly) {
        throw new CoworkHttpError({
          code: "dashboard_read_only",
          message: "Turn off read-only mode before setting up Co-work in this folder.",
          retryable: false,
          status: 403,
        });
      }
      try {
        await this.#initializePendingFolder();
      } catch (error) {
        if (this.#requiresFreshFolderInspection(error) && this.#pendingCandidate !== null) {
          const inspectionEpoch = ++this.#inspectionEpoch;
          try {
            await this.#refreshPendingFolderInspection(inspectionEpoch);
          } catch (refreshError) {
            this.#restoreTransientFolderSelection(inspectionEpoch);
            throw refreshError;
          }
          // A short-lived inspection can expire between the confirmation rendering
          // and the user's click. If the refreshed result still permits setup,
          // complete the confirmed action once with the fresh token. Any second
          // failure falls through to the normal retry state; this is not a loop.
          if (this.#model.folderSelection.kind !== "setup_confirmation") return;
          try {
            await this.#initializePendingFolder();
            return;
          } catch (retryError) {
            if (this.#pendingCandidate !== null) {
              this.#patch(
                {
                  folderSelection: {
                    kind: "setup_available",
                    candidate: this.#pendingCandidate,
                  },
                },
                "folder-initialize-retry-failed",
              );
            }
            throw retryError;
          }
        }
        if (this.#pendingCandidate !== null) {
          this.#patch(
            {
              folderSelection: {
                kind: "setup_available",
                candidate: this.#pendingCandidate,
              },
            },
            "folder-initialize-failed",
          );
        }
        throw error;
      }
    }
  }

  async #inspectWithFailureRecovery(
    input: {
      readonly selectionToken?: string;
      readonly folderPath?: string;
      readonly continuationToken?: string;
    },
    inspectionEpoch: number,
  ): Promise<void> {
    try {
      await this.#inspect(input, inspectionEpoch);
    } catch (error) {
      this.#restoreTransientFolderSelection(inspectionEpoch);
      throw error;
    }
  }

  #restoreTransientFolderSelection(inspectionEpoch: number): void {
    if (inspectionEpoch !== this.#inspectionEpoch) return;
    const kind = this.#model.folderSelection.kind;
    if (kind !== "choosing" && kind !== "inspecting" && kind !== "inspecting_descendants") {
      return;
    }
    this.#patch(
      { folderSelection: this.#selectionBeforeInspection },
      "folder-selection-failed",
    );
  }

  async #inspect(input: {
    readonly selectionToken?: string;
    readonly folderPath?: string;
    readonly continuationToken?: string;
  }, inspectionEpoch = this.#inspectionEpoch, allowPathRefresh = true): Promise<void> {
    if (input.continuationToken === undefined) {
      this.#patch(
        {
          folderSelection: {
            kind: "inspecting",
            candidate: this.#pendingCandidate,
          },
          navigationError: null,
        },
        "folder-inspecting",
      );
    }
    let inspection: CoworkInspectionResult;
    try {
      inspection = await this.#client.inspectFolder(input);
    } catch (error) {
      if (
        allowPathRefresh &&
        this.#requiresFreshFolderInspection(error) &&
        this.#pendingCandidate !== null
      ) {
        await this.#refreshPendingFolderInspection(inspectionEpoch);
        return;
      }
      throw error;
    }
    if (inspectionEpoch !== this.#inspectionEpoch) return;
    if (inspection.inspectionToken !== this.#inspectionToken) {
      this.#folderMutationKey = null;
    }
    this.#inspectionToken = inspection.inspectionToken;
    this.#continuationToken = inspection.continuationToken;
    if (inspection.candidate !== null) this.#pendingCandidate = inspection.candidate;
    if (inspection.status === "inspection_pending") {
      const selection = this.#selectionFromInspection(inspection);
      this.#patch({ folderSelection: selection }, "folder-inspection-continuing");
      const continuationToken = inspection.continuationToken;
      if (continuationToken === null) return;
      const retryAfterMs = Math.min(Math.max(inspection.retryAfterMs ?? 0, 0), 1_000);
      if (retryAfterMs > 0) {
        await new Promise<void>((resolve) => {
          globalThis.setTimeout(resolve, retryAfterMs);
        });
      }
      if (
        inspectionEpoch !== this.#inspectionEpoch ||
        this.#continuationToken !== continuationToken
      ) {
        return;
      }
      await this.#inspect({ continuationToken }, inspectionEpoch, allowPathRefresh);
      return;
    }
    if (inspection.status === "initialized") {
      if (inspection.inspectionToken === null) {
        throw new Error("The initialized folder check expired. Check it again.");
      }
      try {
        await this.#withDurableSessionLeave(null, async () => {
          const folder = await this.#client.openFolder(inspection.inspectionToken!);
          this.#inspectionToken = null;
          this.#model = {
            ...this.#model,
            folders: [
              folder,
              ...this.#model.folders.filter((entry) => entry.storeId !== folder.storeId),
            ],
            folderSelection: { kind: "initialized", folder },
            activeFolderStoreId: folder.storeId,
            routeTarget: { kind: "launcher", storeId: folder.storeId },
            activeSession: { kind: "none" },
            document: null,
          };
          await this.#loadCatalog(folder.storeId);
          this.#location.pushSearch(this.#storeSearch(folder.storeId));
          this.#touch("folder-opened");
        });
      } catch (error) {
        this.#patch(
          { folderSelection: this.#selectionBeforeInspection },
          "folder-open-durability-failed",
        );
        throw error;
      }
      return;
    }
    const selection = this.#selectionFromInspection(inspection);
    this.#patch({ folderSelection: selection }, `folder-inspected:${inspection.status}`);
  }

  async #refreshPendingFolderInspection(inspectionEpoch: number): Promise<void> {
    if (this.#pendingCandidate === null) {
      throw new Error("Choose the folder again.");
    }
    this.#inspectionToken = null;
    this.#folderMutationKey = null;
    this.#continuationToken = null;
    this.#lastInspectionInput = {
      folderPath: this.#pendingCandidate.folderPath,
    };
    await this.#inspect(this.#lastInspectionInput, inspectionEpoch, false);
  }

  #requiresFreshFolderInspection(error: unknown): boolean {
    const code = asCoworkApiError(error).code;
    return code === "selection_expired" || code === "folder_changed";
  }

  async #initializePendingFolder(): Promise<void> {
    if (this.#inspectionToken === null) {
      throw new Error("The folder check expired. Open the folder again.");
    }
    const folder = await this.#client.initializeFolder(
      this.#inspectionToken,
      this.#folderMutationIdempotencyKey(this.#inspectionToken, "initialize"),
    );
    this.#inspectionToken = null;
    this.#folderMutationKey = null;
    this.#continuationToken = null;
    this.#model = {
      ...this.#model,
      folders: [folder, ...this.#model.folders.filter((entry) => entry.storeId !== folder.storeId)],
      folderSelection: { kind: "initialized", folder },
      activeFolderStoreId: folder.storeId,
      routeTarget: { kind: "launcher", storeId: folder.storeId },
      activeSession: { kind: "none" },
      navigationError: null,
      document: null,
    };
    await this.#loadCatalog(folder.storeId);
    this.#location.pushSearch(this.#storeSearch(folder.storeId));
    this.#touch("folder-initialized");
  }

  #selectionFromInspection(inspection: CoworkInspectionResult): CoworkFolderSelection {
    const candidate = inspection.candidate ?? this.#pendingCandidate;
    if (inspection.status === "inspection_pending") {
      return {
        kind: "inspecting_descendants",
        candidate: candidate ?? { folderName: "folder", folderPath: "" },
        progress: inspection.progress ?? { visited: 0, complete: false },
      };
    }
    if (inspection.status === "initialized" && inspection.folder !== null) {
      return { kind: "initialized", folder: inspection.folder };
    }
    if (inspection.status === "uninitialized" && candidate !== null) {
      return { kind: "setup_confirmation", candidate };
    }
    if (
      inspection.status === "inside_existing_folder" &&
      candidate !== null &&
      inspection.owner !== null
    ) {
      return { kind: "inside_existing_folder", candidate, owner: inspection.owner };
    }
    if (inspection.status === "contains_nested_folder" && candidate !== null) {
      return { kind: "contains_nested_folder", candidate, boundaries: inspection.boundaries };
    }
    if (inspection.status === "collision" && candidate !== null) {
      return {
        kind: "store_layout_conflict",
        candidate,
        reasonCode: (inspection.reasonCode ?? "folder_layout_incomplete") as
          | "folder_layout_incomplete"
          | "folder_store_collision"
          | "identity_conflict",
        availableActions: inspection.actions.filter(
          (action): action is "retry" | "inspect" | "open_owner" | "choose_another" =>
            action === "retry" ||
            action === "inspect" ||
            action === "open_owner" ||
            action === "choose_another",
        ),
      };
    }
    return {
      kind: "unavailable",
      candidate,
      reasonCode: (inspection.reasonCode ?? "folder_unreadable") as
        | "folder_not_found"
        | "folder_unreadable"
        | "folder_disallowed"
        | "descendant_scan_incomplete"
        | "folder_too_large_for_safe_setup",
      retryable: inspection.actions.includes("retry"),
    };
  }

  #folderMutationIdempotencyKey(
    inspectionToken: string,
    operation: "initialize",
  ): string {
    const current = this.#folderMutationKey;
    if (
      current !== null &&
      current.inspectionToken === inspectionToken &&
      current.operation === operation
    ) {
      return current.key;
    }
    const key = idempotencyKey();
    this.#folderMutationKey = { inspectionToken, operation, key };
    return key;
  }

  #patch(update: Partial<CoworkViewModel>, reason: string): void {
    this.#model = {
      ...this.#model,
      ...update,
      document:
        update.activeSession === undefined
          ? this.#model.document
          : activeDocument(update.activeSession),
    };
    this.#touch(reason);
  }

  #touch(reason: string): void {
    this.#revision += 1;
    const invalidation: AppInvalidation = {
      id: `cowork:${this.#revision}`,
      appId: COWORK_APP_ID,
      viewIds: [COWORK_VIEW_ID],
      revision: this.#revision,
      reason,
      observedAt: new Date().toISOString(),
    };
    for (const listener of this.#listeners) listener(invalidation);
  }

  #viewSnapshot(): ViewSnapshot<CoworkViewModel> {
    return {
      viewId: COWORK_VIEW_ID,
      revision: this.#revision,
      observedAt: new Date().toISOString(),
      status: this.#model.readOnly ? "read-only" : "ready",
      quality: {
        kind: this.#model.navigationError === null ? "complete" : "partial",
        ...(this.#model.navigationError === null
          ? {}
          : { message: this.#model.navigationError.message }),
      },
      model: this.#model,
      bindings: {},
      widgetInputs: {},
    };
  }
}
