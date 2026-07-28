import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  ChatExecutionSelectionInput,
  ChatExecutionSnapshot,
} from "../../../widget-library/chat";
import type { FeedbackCapture } from "./contracts";
import {
  HttpCoworkDocumentConversationBindingClient,
  CoworkDocumentConversationBindingError,
  normalizeCoworkDocumentAgent,
  type CoworkDocumentAgent,
  type CoworkDocumentConversationBinding,
  type CoworkDocumentConversationBindingClient,
} from "./documentConversationBinding";

export type CoworkConversationBindingPhase =
  | "loading"
  | "idle"
  | "ready"
  | "ensuring"
  | "error";

export interface CoworkConversationBindingState {
  readonly phase: CoworkConversationBindingPhase;
  readonly conversationId: string | null;
  readonly agent: CoworkDocumentAgent;
  readonly feedback: readonly FeedbackCapture[];
  readonly execution?: ChatExecutionSnapshot;
  /** True while a user-authorized start/restart POST is in flight. */
  readonly ensuring: boolean;
  readonly error: string | null;
}

export interface UseDocumentConversationBindingResult
  extends CoworkConversationBindingState {
  /** Present-user-intent mutation: ensure the binding and start/restart its agent. */
  ensure(execution?: ChatExecutionSelectionInput): Promise<void>;
  /** Adopt the authoritative binding returned by a successful feedback POST. */
  adoptFeedback(capture: FeedbackCapture): void;
  /** Adopt an execution PATCH and its atomically returned agent state. */
  adoptExecution(
    sourceDocumentId: string,
    sourceStoreId: string,
    snapshot: ChatExecutionSnapshot,
    agent?: unknown,
    conversationId?: string,
  ): void;
}

const NOT_STARTED_AGENT: CoworkDocumentAgent = {
  status: "not_started",
  alive: null,
  started: false,
  error: null,
};

const initialState = (): CoworkConversationBindingState => ({
  phase: "loading",
  conversationId: null,
  agent: NOT_STARTED_AGENT,
  feedback: [],
  execution: undefined,
  ensuring: false,
  error: null,
});

const errorText = (error: unknown): string =>
  error instanceof Error && error.message.trim().length > 0
    ? error.message
    : "Chat could not be loaded.";

const stateFromBinding = (
  binding: CoworkDocumentConversationBinding,
): CoworkConversationBindingState => ({
  phase: binding.conversationId === null ? "idle" : "ready",
  conversationId: binding.conversationId,
  agent: binding.agent,
  feedback: binding.feedback,
  execution: binding.execution,
  ensuring: false,
  error: null,
});

/**
 * Resolve one document's opaque conversation id without allowing a late
 * response from a previous document to rebind the current one.
 */
export function useDocumentConversationBinding({
  documentId,
  storeId,
  client,
}: {
  readonly documentId: string;
  readonly storeId: string;
  readonly client?: CoworkDocumentConversationBindingClient;
}): UseDocumentConversationBindingResult {
  const resolvedClient = useMemo(
    () => client ?? new HttpCoworkDocumentConversationBindingClient(),
    [client],
  );
  const identity = `${storeId}\u0000${documentId}`;
  const identityRef = useRef(identity);
  identityRef.current = identity;
  const stateIdentityRef = useRef(identity);
  const requestSequence = useRef(0);
  const ensureInFlight = useRef<Promise<void> | null>(null);
  const [state, setState] = useState<CoworkConversationBindingState>(
    initialState,
  );

  useEffect(() => {
    const expectedIdentity = identity;
    const sequence = ++requestSequence.current;
    ensureInFlight.current = null;
    stateIdentityRef.current = expectedIdentity;
    setState(initialState());
    void resolvedClient
      .load(documentId, storeId)
      .then((binding) => {
        if (
          identityRef.current !== expectedIdentity ||
          requestSequence.current !== sequence
        ) {
          return;
        }
        setState(stateFromBinding(binding));
      })
      .catch((error: unknown) => {
        if (
          identityRef.current !== expectedIdentity ||
          requestSequence.current !== sequence
        ) {
          return;
        }
        setState({
          phase: "error",
          conversationId: null,
          agent: NOT_STARTED_AGENT,
          feedback: [],
          ensuring: false,
          error: errorText(error),
        });
      });
  }, [documentId, identity, resolvedClient, storeId]);

  const ensure = useCallback(
    (execution?: ChatExecutionSelectionInput): Promise<void> => {
      if (ensureInFlight.current !== null) return ensureInFlight.current;
      const expectedIdentity = identity;
      const sequence = ++requestSequence.current;
      stateIdentityRef.current = expectedIdentity;
      setState((current) => ({
        ...current,
        // Keep an existing transcript mounted while its agent restarts. Only a
        // first-ever start uses the full-pane ensuring gate.
        phase: current.conversationId === null ? "ensuring" : "ready",
        ensuring: true,
        error: null,
      }));
      const pending = (
        execution === undefined
          ? resolvedClient.ensure(documentId, storeId)
          : resolvedClient.ensure(documentId, storeId, execution)
      )
        .then((binding) => {
          if (
            identityRef.current !== expectedIdentity ||
            requestSequence.current !== sequence
          ) {
            return;
          }
          if (binding.conversationId === null) {
            throw new Error(
              "The server did not return a document conversation.",
            );
          }
          setState(stateFromBinding(binding));
        })
        .catch((error: unknown) => {
          if (
            identityRef.current !== expectedIdentity ||
            requestSequence.current !== sequence
          ) {
            return;
          }
          setState((current) => ({
            // A restart failure must not throw away an already loaded transcript.
            phase: current.conversationId === null ? "error" : "ready",
            conversationId: current.conversationId,
            agent: current.agent,
            feedback: current.feedback,
            execution:
              error instanceof CoworkDocumentConversationBindingError &&
              error.authoritativeExecution !== undefined
                ? error.authoritativeExecution
                : current.execution,
            ensuring: false,
            error: errorText(error),
          }));
        })
        .finally(() => {
          if (
            identityRef.current === expectedIdentity &&
            ensureInFlight.current === pending
          ) {
            ensureInFlight.current = null;
          }
        });
      ensureInFlight.current = pending;
      return pending;
    },
    [documentId, identity, resolvedClient, storeId],
  );

  const adoptFeedback = useCallback(
    (capture: FeedbackCapture): void => {
      if (
        identityRef.current !== identity ||
        capture.documentId !== documentId ||
        capture.storeId !== storeId ||
        capture.conversationId.trim().length === 0
      ) {
        return;
      }
      requestSequence.current += 1;
      ensureInFlight.current = null;
      stateIdentityRef.current = identity;
      setState((current) => {
        if (
          current.conversationId !== null &&
          current.conversationId !== capture.conversationId
        ) {
          return {
            ...current,
            phase: "error",
            error:
              "The document conversation changed unexpectedly. Reload before continuing.",
          };
        }
        return {
          phase: "ready",
          conversationId: capture.conversationId,
          agent: capture.agent ?? NOT_STARTED_AGENT,
          feedback: [
            ...current.feedback.filter(
              (entry) =>
                entry.evidenceId !== capture.evidenceId &&
                entry.messageId !== capture.messageId,
            ),
            capture,
          ],
          execution: capture.execution ?? current.execution,
          ensuring: false,
          error: null,
        };
      });
    },
    [documentId, identity, storeId],
  );

  const adoptExecution = useCallback(
    (
      sourceDocumentId: string,
      sourceStoreId: string,
      snapshot: ChatExecutionSnapshot,
      agent?: unknown,
      conversationId?: string,
    ): void => {
      if (
        identityRef.current !== identity ||
        sourceDocumentId !== documentId ||
        sourceStoreId !== storeId
      ) {
        return;
      }
      requestSequence.current += 1;
      ensureInFlight.current = null;
      stateIdentityRef.current = identity;
      setState((current) => {
        if (
          conversationId !== undefined &&
          current.conversationId !== null &&
          current.conversationId !== conversationId
        ) {
          return {
            ...current,
            phase: "error",
            error:
              "The document conversation changed unexpectedly. Reload before continuing.",
          };
        }
        const nextConversationId =
          conversationId ?? current.conversationId;
        return {
          ...current,
          phase:
            nextConversationId === null ? current.phase : "ready",
          conversationId: nextConversationId,
          execution: snapshot,
          agent:
            agent === undefined
              ? current.agent
              : normalizeCoworkDocumentAgent({ agent }),
          error: null,
        };
      });
    },
    [documentId, identity, storeId],
  );

  const visibleState =
    stateIdentityRef.current === identity ? state : initialState();
  return { ...visibleState, ensure, adoptFeedback, adoptExecution };
}
