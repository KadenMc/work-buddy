// The poll or subscribe hook shape. It binds a ChatConversationProvider to
// React state: an initial load, a silent refresh on every provider
// invalidation, and a send path that surfaces failures without discarding the
// human draft. It holds no transport knowledge, only the seam.

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
  const activeRef = useRef<object>({});
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    const active = {};
    activeRef.current = active;
    let cancelled = false;
    const isCurrent = () => !cancelled && activeRef.current === active;

    const load = (showLoading: boolean) => {
      if (showLoading) {
        setStatus("loading");
        setError(null);
      }
      provider
        .loadConversation(conversationId)
        .then((next) => {
          if (!isCurrent()) return;
          setSnapshot(next);
          setStatus("ready");
          setError(null);
        })
        .catch((cause) => {
          if (!isCurrent()) return;
          // A silent refresh must not blow away a good transcript. Only the
          // initial load (or an explicit retry) escalates to the error state.
          if (showLoading) {
            setStatus("error");
            setError(messageOf(cause));
          }
        });
    };

    load(true);
    const unsubscribe = provider.subscribe(conversationId, () => load(false));

    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [provider, conversationId, reloadToken]);

  const send = useCallback(
    async (value: string, inReplyTo?: string) => {
      setSending(true);
      setSendError(null);
      const input: ChatSendInput = { value, inReplyTo };
      try {
        const next = await provider.sendMessage(conversationId, input);
        if (activeRef.current !== null) {
          setSnapshot(next);
        }
      } catch (cause) {
        setSendError(messageOf(cause));
        throw cause;
      } finally {
        setSending(false);
      }
    },
    [provider, conversationId],
  );

  const retry = useCallback(() => {
    setReloadToken((token) => token + 1);
  }, []);

  return { snapshot, status, error, sending, sendError, send, retry };
}
