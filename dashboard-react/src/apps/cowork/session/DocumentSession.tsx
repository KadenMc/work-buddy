import {
  createContext,
  useCallback,
  useContext,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  useCoworkBridge,
  type CoworkBridge,
  type UseCoworkBridgeOptions,
} from "../bridge";
import type { CoworkSyncStatus } from "../persistence/CoworkYdocPersistence";

export interface RegisteredDocumentRef {
  readonly kind: "workspace";
  readonly storeId: string;
  readonly documentId: string;
}

export interface BoundDocumentRef {
  readonly kind: "domain-bound";
  readonly storeId: string;
  readonly documentId: string;
  readonly binding: {
    readonly bindingId: string;
    readonly domain: {
      readonly namespace: string;
      readonly kind: string;
      readonly entityId: string;
      readonly role: string;
    };
    readonly authorityEpoch: number;
    readonly projectionMode: "none" | "managed_file" | "managed_section";
  };
}

export type DocumentRef = RegisteredDocumentRef | BoundDocumentRef;

export const documentSessionKey = (
  reference: Pick<DocumentRef, "storeId" | "documentId">,
): string => JSON.stringify([reference.storeId, reference.documentId]);

export class DuplicateWritableDocumentSessionError extends Error {
  constructor(readonly sessionKey: string) {
    super(`A writable document session is already registered for ${sessionKey}.`);
    this.name = "DuplicateWritableDocumentSessionError";
  }
}

/**
 * Per-application-window guard for live document runtimes. Presentation hosts
 * share one DocumentSession; constructing a second writable runtime for the
 * same identity fails before its editor can hydrate.
 */
export class DocumentSessionRegistry {
  readonly #writers = new Map<string, { readonly hostId: string; count: number }>();

  register(sessionKey: string, hostId: string, writable: boolean): () => void {
    if (!writable) return () => undefined;
    const current = this.#writers.get(sessionKey);
    if (current !== undefined && current.hostId !== hostId) {
      throw new DuplicateWritableDocumentSessionError(sessionKey);
    }
    if (current === undefined) this.#writers.set(sessionKey, { hostId, count: 1 });
    else current.count += 1;
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const registered = this.#writers.get(sessionKey);
      if (registered?.hostId !== hostId) return;
      registered.count -= 1;
      if (registered.count === 0) this.#writers.delete(sessionKey);
    };
  }

  hasWritable(sessionKey: string): boolean {
    return this.#writers.has(sessionKey);
  }
}

export const documentSessionRegistry = new DocumentSessionRegistry();

export interface DocumentSession {
  readonly key: string;
  readonly reference: RegisteredDocumentRef;
  readonly bridge: CoworkBridge;
  readonly writable: boolean;
  readonly syncStatus: CoworkSyncStatus;
}

export interface UseDocumentSessionOptions extends UseCoworkBridgeOptions {
  readonly registry?: DocumentSessionRegistry;
}

/** Own one live bridge and publish presentation-safe session state. */
export function useDocumentSession({
  registry = documentSessionRegistry,
  ...options
}: UseDocumentSessionOptions): DocumentSession {
  const callbackRef = useRef(options.onSyncStatus);
  callbackRef.current = options.onSyncStatus;
  const [syncStatus, setSyncStatus] = useState<CoworkSyncStatus>(
    options.readOnly === true ? "read_only" : "hydrating",
  );
  const onSyncStatus = useCallback((status: CoworkSyncStatus): void => {
    setSyncStatus(status);
    callbackRef.current?.(status);
  }, []);
  const bridge = useCoworkBridge({ ...options, onSyncStatus });
  const reference = useMemo<RegisteredDocumentRef>(
    () => ({
      kind: "workspace",
      storeId: options.storeId,
      documentId: options.documentId,
    }),
    [options.documentId, options.storeId],
  );
  const key = documentSessionKey(reference);
  const hostId = useId();
  const writable = options.readOnly !== true;

  // A layout effect runs before the editor's passive hydration/persistence
  // effects, so duplicate writable compositions fail closed.
  useLayoutEffect(
    () => registry.register(key, hostId, writable),
    [hostId, key, registry, writable],
  );

  return useMemo(
    () => ({ key, reference, bridge, writable, syncStatus }),
    [bridge, key, reference, syncStatus, writable],
  );
}

const DocumentSessionContext = createContext<DocumentSession | null>(null);

export function DocumentSessionProvider({
  session,
  children,
}: {
  readonly session: DocumentSession;
  readonly children: ReactNode;
}) {
  const parent = useContext(DocumentSessionContext);
  if (parent !== null && parent.key === session.key && parent !== session) {
    throw new DuplicateWritableDocumentSessionError(session.key);
  }
  if (parent === session) return children;
  return (
    <DocumentSessionContext.Provider value={session}>
      {children}
    </DocumentSessionContext.Provider>
  );
}

export function useDocumentSessionContext(): DocumentSession {
  const session = useContext(DocumentSessionContext);
  if (session === null) {
    throw new Error("DocumentEditorSurface requires a DocumentSessionProvider.");
  }
  return session;
}
