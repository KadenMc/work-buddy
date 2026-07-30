import {
  createContext,
  useContext,
  useMemo,
  type ReactNode,
} from "react";

import type { CoworkActionSnapshotController } from "../targets";
import type { CoworkDocumentAgent } from "./documentConversationBinding";
import {
  HttpCoworkChatActionSnapshotClient,
  type CoworkChatActionSnapshotClient,
} from "./HttpCoworkChatActionSnapshotClient";

export interface CoworkChatTargeting {
  readonly storeId: string;
  readonly documentId: string;
  readonly controller: CoworkActionSnapshotController | null;
  readonly agent: CoworkDocumentAgent;
  readonly client: CoworkChatActionSnapshotClient;
}

const TargetingContext = createContext<CoworkChatTargeting | null>(null);

/**
 * Workspace-to-Chat extension seam. The rail remains unaware of editor capture
 * internals; only the Co-work Chat adapter opts into this document context.
 */
export function CoworkChatTargetingProvider({
  storeId,
  documentId,
  controller,
  agent,
  client,
  children,
}: {
  readonly storeId: string;
  readonly documentId: string;
  readonly controller: CoworkActionSnapshotController | null;
  readonly agent: CoworkDocumentAgent;
  readonly client?: CoworkChatActionSnapshotClient;
  readonly children: ReactNode;
}) {
  const defaultClient = useMemo(
    () => new HttpCoworkChatActionSnapshotClient({ storeId, documentId }),
    [documentId, storeId],
  );
  const value = useMemo<CoworkChatTargeting>(
    () => ({
      storeId,
      documentId,
      controller,
      agent,
      client: client ?? defaultClient,
    }),
    [
      agent,
      client,
      controller,
      defaultClient,
      documentId,
      storeId,
    ],
  );
  return (
    <TargetingContext.Provider value={value}>
      {children}
    </TargetingContext.Provider>
  );
}

export const useOptionalCoworkChatTargeting =
  (): CoworkChatTargeting | null => useContext(TargetingContext);
