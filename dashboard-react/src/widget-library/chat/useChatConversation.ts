// The poll or subscribe hook shape. It binds a ChatConversationProvider to
// React state: an initial load, a silent refresh on every provider
// invalidation, and a send path that surfaces failures without discarding the
// human draft. It holds no transport knowledge, only the seam.
//
// The provider argument must be referentially stable (module constant, memo,
// or context value). A consumer that constructs a fresh provider each render
// re-subscribes and reloads on every render.

import { useCallback, useEffect, useRef, useState } from "react";

import type {
  ChatConversationProvider,
  ChatConversationSnapshot,
  ChatSendInput,
} from "./contracts";

/** Load lifecycle for the whole transcript. Send failures are separate. */
export type ChatLoadStatus = "loading" | "ready" | "error";

export interface UseChatConversationResult {
  readonly snapshot: ChatConversationSnapshot | null;
  readonly status: ChatLoadStatus;
  /** Set when status is "error", a human-readable load failure reason. */
  readonly error: string | null;
  readonly sending: boolean;
  /** Set when the most recent send failed, cleared on the next attempt. */
  readonly sendError: string | null;
  /** Submit a human message or answer. Rejects on failure so a composer can retain its draft. */
  send(value: string, inReplyTo?: string): Promise<void>;
  /** Re-run the initial load after an error. */
  retry(): void;
}

function messageOf(error: unknown): string {
  if (error instanceof Error && error.message.length > 0) return error.message;
  return "Something went wrong.";
}

interface ActiveChatBinding {
  readonly provider: ChatConversationProvider;
  readonly conversationId: string;
  cancelled: boolean;
  sequence: number;
  appliedSequence: number;
  discardLoadsBefore: number;
  loadInFlight: boolean;
  sendsInFlight: number;
  latestSendSequence: number;
  refreshQueued: boolean;
  refresh: (() => void) | null;
}

const newActiveBinding = (
  provider: ChatConversationProvider,
  conversationId: string,
): ActiveChatBinding => ({
  provider,
  conversationId,
  cancelled: false,
  sequence: 0,
  appliedSequence: 0,
  discardLoadsBefore: 0,
  loadInFlight: false,
  sendsInFlight: 0,
  latestSendSequence: 0,
  refreshQueued: false,
  refresh: null,
});

export function useChatConversation(
  provider: ChatConversationProvider,
  conversationId: string,
): UseChatConversationResult {
  const [snapshot, setSnapshot] = useState<ChatConversationSnapshot | null>(
    null,
  );
  const [status, setStatus] = useState<ChatLoadStatus>("loading");
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  // Identity of the active binding. Async results from a superseded provider or
  // conversation (or after unmount) are dropped rather than applied.
  const activeRef = useRef<ActiveChatBinding | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    const active = newActiveBinding(provider, conversationId);
    activeRef.current = active;
    const isCurrent = () =>
      !active.cancelled && activeRef.current === active;

    setSnapshot(null);
    setSending(false);
    setSendError(null);

    const flushQueuedRefresh = () => {
      if (
        !isCurrent() ||
        !active.refreshQueued ||
        active.loadInFlight ||
        active.sendsInFlight > 0
      ) {
        return;
      }
      active.refreshQueued = false;
      active.refresh?.();
    };

    const load = (showLoading: boolean) => {
      if (active.loadInFlight || active.sendsInFlight > 0) {
        active.refreshQueued = true;
        return;
      }
      active.loadInFlight = true;
      const sequence = ++active.sequence;
      if (showLoading) {
        setStatus("loading");
        setError(null);
      }
      provider
        .loadConversation(conversationId)
        .then((next) => {
          if (
            !isCurrent() ||
            sequence < active.discardLoadsBefore ||
            sequence < active.appliedSequence
          ) {
            return;
          }
          active.appliedSequence = sequence;
          setSnapshot(next);
          setStatus("ready");
          setError(null);
        })
        .catch((cause) => {
          if (
            !isCurrent() ||
            sequence < active.discardLoadsBefore ||
            sequence < active.appliedSequence
          ) {
            return;
          }
          // A silent refresh must not blow away a good transcript. Only the
          // initial load (or an explicit retry) escalates to the error state.
          if (showLoading) {
            setStatus("error");
            setError(messageOf(cause));
          }
        })
        .finally(() => {
          if (!isCurrent()) return;
          active.loadInFlight = false;
          flushQueuedRefresh();
        });
    };

    active.refresh = () => load(false);
    load(true);
    const unsubscribe = provider.subscribe(conversationId, () => {
      active.refresh?.();
    });

    return () => {
      active.cancelled = true;
      unsubscribe();
    };
  }, [provider, conversationId, reloadToken]);

  const send = useCallback(
    async (value: string, inReplyTo?: string) => {
      // Capture the binding this send belongs to. A send resolving after the
      // hook has rebound to another provider or conversation must not write
      // its snapshot or error over the current binding's state.
      const active = activeRef.current;
      if (
        active === null ||
        active.cancelled ||
        active.provider !== provider ||
        active.conversationId !== conversationId
      ) {
        throw new Error("Chat is not ready.");
      }
      const sequence = ++active.sequence;
      active.latestSendSequence = sequence;
      active.discardLoadsBefore = sequence;
      active.sendsInFlight += 1;
      if (active.loadInFlight) active.refreshQueued = true;
      setSending(true);
      setSendError(null);
      const input: ChatSendInput = { value, inReplyTo };
      try {
        const next = await provider.sendMessage(conversationId, input);
        if (
          activeRef.current === active &&
          !active.cancelled &&
          sequence === active.latestSendSequence &&
          sequence >= active.appliedSequence
        ) {
          active.appliedSequence = sequence;
          setSnapshot(next);
          setStatus("ready");
          setError(null);
        }
      } catch (cause) {
        if (
          activeRef.current === active &&
          !active.cancelled &&
          sequence === active.latestSendSequence
        ) {
          setSendError(messageOf(cause));
        }
        throw cause;
      } finally {
        if (activeRef.current === active && !active.cancelled) {
          active.sendsInFlight = Math.max(0, active.sendsInFlight - 1);
          setSending(active.sendsInFlight > 0);
          if (
            active.sendsInFlight === 0 &&
            active.refreshQueued &&
            !active.loadInFlight
          ) {
            active.refreshQueued = false;
            active.refresh?.();
          }
        }
      }
    },
    [provider, conversationId],
  );

  const retry = useCallback(() => {
    setReloadToken((token) => token + 1);
  }, []);

  return { snapshot, status, error, sending, sendError, send, retry };
}
