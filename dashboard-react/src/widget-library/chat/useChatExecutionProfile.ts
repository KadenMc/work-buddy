import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  ChatExecutionProfileProvider,
  ChatExecutionSelectionInput,
  ChatExecutionSnapshot,
} from "./contracts";

export type ChatExecutionLoadStatus =
  | "loading"
  | "ready"
  | "error"
  | "unavailable";

export interface ChatExecutionSelectionCandidate {
  readonly providerId: string;
  readonly modelId: string;
  readonly providerLabel: string;
  readonly modelLabel: string;
}

/** Host-authored consequence copy for a selection that needs confirmation. */
export interface ChatExecutionSwitchConfirmation {
  readonly title: string;
  readonly description: string;
  readonly confirmLabel: string;
  readonly cancelLabel?: string;
}

export type ChatExecutionConfirmSelection = (
  selection: ChatExecutionSelectionCandidate,
) => ChatExecutionSwitchConfirmation | null;

/**
 * Transport errors may carry the server's newer authoritative snapshot. The
 * hook adopts it while still reporting that the requested switch did not land.
 */
export class ChatExecutionSelectionError extends Error {
  constructor(
    message: string,
    readonly authoritativeSnapshot?: ChatExecutionSnapshot,
  ) {
    super(message);
    this.name = "ChatExecutionSelectionError";
  }
}

export interface ChatExecutionControl {
  readonly snapshot: ChatExecutionSnapshot | null;
  readonly status: ChatExecutionLoadStatus;
  readonly selecting: boolean;
  readonly error: string | null;
  readonly announcement: string | null;
  readonly currentAvailable: boolean;
  /**
   * Optional host policy. Returning copy asks the shared picker to confirm;
   * returning null keeps low-impact choices immediate.
   */
  readonly confirmSelection?: ChatExecutionConfirmSelection;
  select(providerId: string, modelId: string): Promise<void>;
  retry(): void;
}

interface ActiveExecutionBinding {
  readonly provider: ChatExecutionProfileProvider;
  readonly targetId: string;
  cancelled: boolean;
  sequence: number;
  appliedSequence: number;
  selectingSequence: number | null;
  deferredReload: boolean;
  reload: (() => void) | null;
}

function messageOf(error: unknown): string {
  if (error instanceof Error && error.message.trim().length > 0) {
    return error.message;
  }
  return "The model selection could not be changed.";
}

export function isCurrentExecutionAvailable(
  snapshot: ChatExecutionSnapshot | null,
): boolean {
  if (snapshot === null) return false;
  const provider = snapshot.providers.find(
    (candidate) => candidate.id === snapshot.selection.providerId,
  );
  const model = provider?.models.find(
    (candidate) => candidate.id === snapshot.selection.modelId,
  );
  return provider?.available === true && model?.available === true;
}

/**
 * Bind an opaque execution target to the reusable picker state. Async results
 * are fenced by provider and target identity, mirroring the transcript hook.
 */
export function useChatExecutionProfile(
  provider: ChatExecutionProfileProvider | null | undefined,
  targetId: string,
): ChatExecutionControl | undefined {
  const [snapshot, setSnapshot] = useState<ChatExecutionSnapshot | null>(null);
  const [status, setStatus] =
    useState<ChatExecutionLoadStatus>("unavailable");
  const [selecting, setSelecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const activeRef = useRef<ActiveExecutionBinding | null>(null);

  useEffect(() => {
    if (provider === null || provider === undefined) {
      activeRef.current = null;
      setSnapshot(null);
      setStatus("unavailable");
      setSelecting(false);
      setError(null);
      setAnnouncement(null);
      return;
    }

    const active: ActiveExecutionBinding = {
      provider,
      targetId,
      cancelled: false,
      sequence: 0,
      appliedSequence: 0,
      selectingSequence: null,
      deferredReload: false,
      reload: null,
    };
    activeRef.current = active;
    const isCurrent = () =>
      !active.cancelled && activeRef.current === active;

    setSnapshot(null);
    setStatus("loading");
    setSelecting(false);
    setError(null);
    setAnnouncement(null);

    const load = (showLoading: boolean) => {
      const selectingSequence = active.selectingSequence;
      if (selectingSequence !== null) {
        active.deferredReload = true;
      }
      const sequence = ++active.sequence;
      if (showLoading) {
        setStatus("loading");
        setError(null);
      }
      provider
        .load(targetId)
        .then((next) => {
          // A subscription reload issued during a selection is observationally
          // older than that mutation, even if its promise resolves first. The
          // PATCH response is the authoritative projection; on failure we
          // issue a fresh reload after the mutation settles.
          if (selectingSequence !== null) return;
          if (
            !isCurrent() ||
            sequence < active.appliedSequence
          ) {
            return;
          }
          active.appliedSequence = sequence;
          setSnapshot(next);
          setStatus("ready");
          setError(null);
        })
        .catch((cause: unknown) => {
          if (selectingSequence !== null) return;
          if (!isCurrent() || sequence < active.appliedSequence) return;
          if (showLoading) {
            setSnapshot(null);
            setStatus("error");
          }
          setError(messageOf(cause));
        });
    };
    active.reload = () => load(false);

    load(true);
    const unsubscribe = provider.subscribe(targetId, () => load(false));
    return () => {
      active.cancelled = true;
      active.reload = null;
      unsubscribe();
    };
  }, [provider, reloadToken, targetId]);

  const select = useCallback(
    async (providerId: string, modelId: string): Promise<void> => {
      const active = activeRef.current;
      if (
        provider === null ||
        provider === undefined ||
        active === null ||
        active.cancelled ||
        active.provider !== provider ||
        active.targetId !== targetId
      ) {
        throw new Error("Model selection is not ready.");
      }
      const current = snapshot;
      if (
        current === null ||
        status !== "ready" ||
        active.selectingSequence !== null
      ) {
        throw new Error("Model selection is not ready.");
      }
      const providerOption = current.providers.find(
        (candidate) => candidate.id === providerId,
      );
      const modelOption = providerOption?.models.find(
        (candidate) => candidate.id === modelId,
      );
      if (
        providerOption?.available !== true ||
        modelOption?.available !== true ||
        current.readOnly === true
      ) {
        throw new Error("That provider and model are not available.");
      }
      if (
        providerId === current.selection.providerId &&
        modelId === current.selection.modelId
      ) {
        return;
      }

      const sequence = ++active.sequence;
      const input: ChatExecutionSelectionInput = {
        providerId,
        modelId,
        expectedRevision: current.selection.revision,
      };
      active.selectingSequence = sequence;
      setSelecting(true);
      setError(null);
      setAnnouncement(null);
      let selectionReconciled = false;
      try {
        const next = await provider.select(targetId, input);
        if (
          activeRef.current !== active ||
          active.cancelled ||
          sequence < active.appliedSequence
        ) {
          return;
        }
        active.appliedSequence = sequence;
        active.deferredReload = false;
        selectionReconciled = true;
        setSnapshot(next);
        setStatus("ready");
        setAnnouncement(
          `Now using ${next.selection.providerLabel} · ${next.selection.modelLabel}.`,
        );
      } catch (cause: unknown) {
        if (
          activeRef.current === active &&
          !active.cancelled &&
          sequence >= active.appliedSequence
        ) {
          if (
            cause instanceof ChatExecutionSelectionError &&
            cause.authoritativeSnapshot !== undefined
          ) {
            active.appliedSequence = sequence;
            active.deferredReload = false;
            selectionReconciled = true;
            setSnapshot(cause.authoritativeSnapshot);
            setStatus("ready");
          }
          setError(messageOf(cause));
        }
        throw cause;
      } finally {
        if (
          activeRef.current === active &&
          !active.cancelled &&
          active.selectingSequence === sequence
        ) {
          const reload =
            active.deferredReload && !selectionReconciled
              ? active.reload
              : null;
          active.deferredReload = false;
          active.selectingSequence = null;
          setSelecting(false);
          reload?.();
        }
      }
    },
    [provider, snapshot, status, targetId],
  );

  const retry = useCallback(() => {
    provider?.refresh?.(targetId);
    setReloadToken((token) => token + 1);
  }, [provider, targetId]);

  const control = useMemo(
    () => ({
      snapshot,
      status,
      selecting,
      error,
      announcement,
      currentAvailable: isCurrentExecutionAvailable(snapshot),
      select,
      retry,
    }),
    [announcement, error, select, selecting, snapshot, status, retry],
  );
  return provider === null || provider === undefined ? undefined : control;
}
