import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import type {
  JsonValue,
  ViewId,
  WidgetDefinition,
  WidgetDraftDeclaration,
  WidgetInstanceId,
} from "../contributions/contracts";
import {
  WidgetDraftConflictError,
  type WidgetDraftIdentity,
  type WidgetDraftRepository,
  widgetDraftStorageKey,
} from "./contracts";
import {
  ForkedWidgetDraftRepository,
  InMemoryWidgetDraftRepository,
} from "./repository";

export type WidgetDraftStatus =
  | "loading"
  | "pristine"
  | "saving"
  | "saved"
  | "conflict"
  | "error";

interface WidgetDraftRuntimeValue {
  readonly profileId: string;
  readonly workspaceId: string;
  readonly deviceRepository: WidgetDraftRepository;
  readonly volatileRepository: WidgetDraftRepository;
}

const WidgetDraftRuntimeContext = createContext<WidgetDraftRuntimeValue | null>(null);

export function WidgetDraftRuntimeProvider({
  children,
  repository,
  profileId = "local-profile",
  workspaceId = "default-workspace",
}: {
  readonly children: ReactNode;
  readonly repository: WidgetDraftRepository;
  readonly profileId?: string;
  readonly workspaceId?: string;
}) {
  const volatileRepository = useMemo(() => new InMemoryWidgetDraftRepository(), []);
  const value = useMemo(
    () => ({
      profileId,
      workspaceId,
      deviceRepository: repository,
      volatileRepository,
    }),
    [profileId, repository, volatileRepository, workspaceId],
  );
  return (
    <WidgetDraftRuntimeContext.Provider value={value}>
      {children}
    </WidgetDraftRuntimeContext.Provider>
  );
}

interface DraftRegistration {
  readonly draftName: string;
  readonly status: WidgetDraftStatus;
  readonly dirty: boolean;
  clear(): Promise<boolean>;
}

interface WidgetDraftScopeValue {
  readonly definition: WidgetDefinition;
  identityFor(draftName: string): WidgetDraftIdentity;
  declarationFor(draftName: string): WidgetDraftDeclaration;
  repositoryFor(draftName: string): WidgetDraftRepository;
  updateRegistration(registration: DraftRegistration): void;
  removeRegistration(draftName: string): void;
  readonly registrations: ReadonlyMap<string, DraftRegistration>;
  clearAll(): Promise<boolean>;
}

const WidgetDraftScopeContext = createContext<WidgetDraftScopeValue | null>(null);

const readPath = (value: unknown, path: readonly string[]): unknown =>
  path.reduce<unknown>((current, part) => {
    if (typeof current !== "object" || current === null || !(part in current)) {
      return undefined;
    }
    return (current as Record<string, unknown>)[part];
  }, value);

export const resolveWidgetDraftScopeKey = (
  declaration: WidgetDraftDeclaration,
  input: unknown,
): string => {
  if (declaration.scope.kind === "view") return "view";
  const value = readPath(input, declaration.scope.path);
  if (typeof value !== "string" && typeof value !== "number") {
    throw new Error(
      `Draft ${declaration.draftName} could not resolve scope field ${declaration.scope.path.join(".")}`,
    );
  }
  return String(value);
};

export function WidgetDraftScopeProvider({
  children,
  definition,
  viewId,
  instanceId,
  input,
  persistenceMode = "normal",
}: {
  readonly children: ReactNode;
  readonly definition: WidgetDefinition;
  readonly viewId: ViewId;
  readonly instanceId: WidgetInstanceId;
  readonly input: unknown;
  readonly persistenceMode?: "normal" | "ephemeral";
}) {
  const runtime = useContext(WidgetDraftRuntimeContext);
  if (runtime === null) {
    throw new Error("WidgetDraftScopeProvider requires WidgetDraftRuntimeProvider");
  }
  const [registrations, setRegistrations] = useState<
    ReadonlyMap<string, DraftRegistration>
  >(() => new Map());
  const registrationsRef = useRef(registrations);
  registrationsRef.current = registrations;
  const previewDeviceRepository = useMemo(
    () => new ForkedWidgetDraftRepository(runtime.deviceRepository),
    [runtime.deviceRepository],
  );
  const previewVolatileRepository = useMemo(
    () => new ForkedWidgetDraftRepository(runtime.volatileRepository),
    [runtime.volatileRepository],
  );

  const declarations = useMemo(
    () => new Map((definition.drafts ?? []).map((draft) => [draft.draftName, draft])),
    [definition.drafts],
  );
  const declarationFor = useCallback(
    (draftName: string) => {
      const declaration = declarations.get(draftName);
      if (declaration === undefined) {
        throw new Error(`${definition.typeId} did not declare draft ${draftName}`);
      }
      return declaration;
    },
    [declarations, definition.typeId],
  );
  const identityFor = useCallback(
    (draftName: string): WidgetDraftIdentity => {
      const declaration = declarationFor(draftName);
      return {
        profileId: runtime.profileId,
        workspaceId: runtime.workspaceId,
        appId: definition.publisherAppId,
        viewId,
        instanceId,
        widgetTypeId: definition.typeId,
        draftName,
        scopeKey: resolveWidgetDraftScopeKey(declaration, input),
      };
    },
    [declarationFor, definition.publisherAppId, definition.typeId, input, instanceId, runtime.profileId, runtime.workspaceId, viewId],
  );
  const repositoryFor = useCallback(
    (draftName: string) => {
      const device = declarationFor(draftName).persistence === "device";
      if (persistenceMode === "ephemeral") {
        return device ? previewDeviceRepository : previewVolatileRepository;
      }
      return device ? runtime.deviceRepository : runtime.volatileRepository;
    },
    [
      declarationFor,
      persistenceMode,
      previewDeviceRepository,
      previewVolatileRepository,
      runtime.deviceRepository,
      runtime.volatileRepository,
    ],
  );
  const updateRegistration = useCallback((registration: DraftRegistration) => {
    setRegistrations((current) => {
      const prior = current.get(registration.draftName);
      if (
        prior?.dirty === registration.dirty &&
        prior.status === registration.status &&
        prior.clear === registration.clear
      ) {
        return current;
      }
      const next = new Map(current);
      next.set(registration.draftName, registration);
      return next;
    });
  }, []);
  const removeRegistration = useCallback((draftName: string) => {
    setRegistrations((current) => {
      if (!current.has(draftName)) return current;
      const next = new Map(current);
      next.delete(draftName);
      return next;
    });
  }, []);
  const clearAll = useCallback(async () => {
    const results = await Promise.all(
      [...registrationsRef.current.values()]
        .filter((registration) => registration.dirty)
        .map((registration) => registration.clear()),
    );
    return results.every(Boolean);
  }, []);
  const value = useMemo<WidgetDraftScopeValue>(
    () => ({
      definition,
      identityFor,
      declarationFor,
      repositoryFor,
      updateRegistration,
      removeRegistration,
      registrations,
      clearAll,
    }),
    [clearAll, declarationFor, definition, identityFor, registrations, removeRegistration, repositoryFor, updateRegistration],
  );
  return (
    <WidgetDraftScopeContext.Provider value={value}>
      {children}
    </WidgetDraftScopeContext.Provider>
  );
}

export interface WidgetDraftHandle<Value> {
  readonly value: Value;
  readonly revision: number;
  readonly ready: boolean;
  readonly dirty: boolean;
  readonly status: WidgetDraftStatus;
  readonly error?: string;
  setValue(next: Value | ((current: Value) => Value)): number;
  flush(): Promise<void>;
  clear(options?: { readonly ifRevision?: number }): Promise<boolean>;
}

interface DraftState<Value> {
  readonly value: Value;
  readonly revision: number;
  readonly ready: boolean;
  readonly status: WidgetDraftStatus;
  readonly error?: string;
}

const asJsonValue = (value: unknown): { readonly value: JsonValue; readonly bytes: number } => {
  const serialized = JSON.stringify(value);
  if (serialized === undefined) throw new Error("Widget drafts must be JSON values");
  return {
    value: JSON.parse(serialized) as JsonValue,
    bytes: new TextEncoder().encode(serialized).byteLength,
  };
};

export function useWidgetDraft<Value>(
  draftName: string,
  initialValue: Value,
  options: { readonly isPristine?: (value: Value) => boolean } = {},
): WidgetDraftHandle<Value> {
  const scope = useContext(WidgetDraftScopeContext);
  if (scope === null) throw new Error("useWidgetDraft must run inside WidgetDraftScopeProvider");
  const declaration = useMemo(
    () => scope.declarationFor(draftName),
    [draftName, scope.declarationFor],
  );
  const identity = useMemo(
    () => scope.identityFor(draftName),
    [draftName, scope.identityFor],
  );
  const repository = useMemo(
    () => scope.repositoryFor(draftName),
    [draftName, scope.repositoryFor],
  );
  const { updateRegistration, removeRegistration } = scope;
  const identityKey = widgetDraftStorageKey(identity);
  const initialValueRef = useRef(initialValue);
  const isPristineRef = useRef(options.isPristine ?? (() => false));
  isPristineRef.current = options.isPristine ?? (() => false);
  const [state, setState] = useState<DraftState<Value>>({
    value: initialValue,
    revision: 0,
    ready: false,
    status: "loading",
  });
  const stateRef = useRef(state);
  const persistedRevisionRef = useRef<number | undefined>(undefined);
  const writeChainRef = useRef<Promise<void>>(Promise.resolve());
  const commitState = useCallback((next: DraftState<Value>) => {
    stateRef.current = next;
    setState(next);
  }, []);

  useEffect(() => {
    let active = true;
    const loading: DraftState<Value> = {
      value: initialValueRef.current,
      revision: 0,
      ready: false,
      status: "loading",
    };
    persistedRevisionRef.current = undefined;
    writeChainRef.current = Promise.resolve();
    commitState(loading);
    void repository
      .load(identity)
      .then((stored) => {
        if (!active) return;
        if (
          stored !== undefined &&
          (stored.draftSchema.schemaId !== declaration.schema.schemaId ||
            stored.draftSchema.version !== declaration.schema.version)
        ) {
          commitState({
            ...loading,
            ready: true,
            status: "error",
            error: "An older draft needs recovery before this widget can edit it.",
          });
          return;
        }
        persistedRevisionRef.current = stored?.revision;
        const value = (stored?.value as Value | undefined) ?? initialValueRef.current;
        commitState({
          value,
          revision: stored?.revision ?? 0,
          ready: true,
          status: stored === undefined ? "pristine" : "saved",
        });
      })
      .catch((error: unknown) => {
        if (!active) return;
        commitState({
          ...loading,
          ready: true,
          status: "error",
          error: `Draft storage is unavailable: ${error instanceof Error ? error.message : String(error)}`,
        });
      });
    return () => {
      active = false;
    };
  }, [commitState, declaration.schema.schemaId, declaration.schema.version, identityKey, repository]);

  const enqueueValue = useCallback(
    (value: Value, localRevision: number, dirty: boolean) => {
      writeChainRef.current = writeChainRef.current
        .catch(() => undefined)
        .then(async () => {
          if (!dirty) {
            await repository.delete(identity, persistedRevisionRef.current);
            persistedRevisionRef.current = undefined;
          } else {
            const serialized = asJsonValue(value);
            if (serialized.bytes > declaration.maxBytes) {
              throw new Error(`Draft exceeds its ${declaration.maxBytes}-byte limit`);
            }
            const saved = await repository.save({
              ...identity,
              draftSchema: declaration.schema,
              value: serialized.value,
              expectedRevision: persistedRevisionRef.current,
              retentionDays: declaration.retentionDays,
            });
            persistedRevisionRef.current = saved.revision;
          }
          if (stateRef.current.revision === localRevision) {
            commitState({
              ...stateRef.current,
              status: dirty ? "saved" : "pristine",
              error: undefined,
            });
          }
        })
        .catch((error: unknown) => {
          if (stateRef.current.revision !== localRevision) return;
          commitState({
            ...stateRef.current,
            status: error instanceof WidgetDraftConflictError ? "conflict" : "error",
            error: error instanceof Error ? error.message : String(error),
          });
        });
    },
    [commitState, declaration.maxBytes, declaration.retentionDays, declaration.schema, identity, repository],
  );

  const setValue = useCallback(
    (nextValue: Value | ((current: Value) => Value)): number => {
      const current = stateRef.current;
      const value =
        typeof nextValue === "function"
          ? (nextValue as (current: Value) => Value)(current.value)
          : nextValue;
      const revision = current.revision + 1;
      const dirty = !isPristineRef.current(value);
      commitState({ value, revision, ready: true, status: "saving" });
      enqueueValue(value, revision, dirty);
      return revision;
    },
    [commitState, enqueueValue],
  );
  const flush = useCallback(async () => {
    await writeChainRef.current;
    if (
      stateRef.current.status === "error" ||
      stateRef.current.status === "conflict"
    ) {
      throw new Error(stateRef.current.error ?? "Draft could not be persisted");
    }
  }, []);
  const clear = useCallback(
    async (clearOptions?: { readonly ifRevision?: number }): Promise<boolean> => {
      const current = stateRef.current;
      if (
        clearOptions?.ifRevision !== undefined &&
        clearOptions.ifRevision !== current.revision
      ) {
        return false;
      }
      const revision = current.revision + 1;
      commitState({
        value: initialValueRef.current,
        revision,
        ready: true,
        status: "saving",
      });
      enqueueValue(initialValueRef.current, revision, false);
      await writeChainRef.current;
      return stateRef.current.status === "pristine";
    },
    [commitState, enqueueValue],
  );
  const dirty = state.ready && !isPristineRef.current(state.value);

  useEffect(() => {
    updateRegistration({ draftName, status: state.status, dirty, clear });
    return () => removeRegistration(draftName);
  }, [clear, dirty, draftName, removeRegistration, state.status, updateRegistration]);

  return {
    ...state,
    dirty,
    setValue,
    flush,
    clear,
  };
}

export function useWidgetDraftScopeStatus(): {
  readonly hasDirtyDraft: boolean;
  readonly dirtyDraftNames: readonly string[];
  clearAll(): Promise<boolean>;
} {
  const scope = useContext(WidgetDraftScopeContext);
  if (scope === null) {
    return { hasDirtyDraft: false, dirtyDraftNames: [], clearAll: async () => true };
  }
  const dirtyDraftNames = [...scope.registrations.values()]
    .filter((registration) => registration.dirty)
    .map((registration) => registration.draftName);
  return {
    hasDirtyDraft: dirtyDraftNames.length > 0,
    dirtyDraftNames,
    clearAll: scope.clearAll,
  };
}
