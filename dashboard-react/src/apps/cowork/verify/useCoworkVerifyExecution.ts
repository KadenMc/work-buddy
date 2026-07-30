import { useCallback, useEffect, useMemo, useState } from "react";

import {
  isCurrentExecutionAvailable,
  type ChatExecutionControl,
} from "../../../widget-library/chat";

interface VerifySelection {
  readonly identity: string;
  readonly providerId: string;
  readonly modelId: string;
}
/**
 * Reuses the account-backed execution catalog without reusing Chat's mutable
 * selection. Choosing a Verify coordinator model must not restart the document
 * conversation, and changing Chat later must not silently change a Verify
 * choice the person has already seen.
 */
export function useCoworkVerifyExecution(
  source: ChatExecutionControl | undefined,
  identity: string,
): ChatExecutionControl | undefined {
  const [selected, setSelected] = useState<VerifySelection | null>(null);
  const [announcement, setAnnouncement] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const sourceSnapshot = source?.snapshot ?? null;

  useEffect(() => {
    setSelected(null);
    setAnnouncement(null);
    setError(null);
  }, [identity]);

  useEffect(() => {
    if (
      sourceSnapshot === null ||
      (selected !== null && selected.identity === identity)
    ) {
      return;
    }
    setSelected({
      identity,
      providerId: sourceSnapshot.selection.providerId,
      modelId: sourceSnapshot.selection.modelId,
    });
  }, [identity, selected, sourceSnapshot]);

  const activeSelection =
    selected?.identity === identity ? selected : null;
  const snapshot = useMemo(() => {
    if (sourceSnapshot === null) return null;
    const providerId =
      activeSelection?.providerId ?? sourceSnapshot.selection.providerId;
    const modelId =
      activeSelection?.modelId ?? sourceSnapshot.selection.modelId;
    const provider = sourceSnapshot.providers.find(
      (candidate) => candidate.id === providerId,
    );
    const model = provider?.models.find(
      (candidate) => candidate.id === modelId,
    );
    return {
      ...sourceSnapshot,
      selection: {
        ...sourceSnapshot.selection,
        providerId,
        modelId,
        providerLabel: provider?.label ?? providerId,
        modelLabel: model?.label ?? modelId,
      },
    };
  }, [activeSelection, sourceSnapshot]);

  const select = useCallback(
    async (providerId: string, modelId: string): Promise<void> => {
      if (snapshot === null || source?.status !== "ready") {
        throw new Error("Verify model selection is not ready.");
      }
      const provider = snapshot.providers.find(
        (candidate) => candidate.id === providerId,
      );
      const model = provider?.models.find(
        (candidate) => candidate.id === modelId,
      );
      if (
        provider?.available !== true ||
        model?.available !== true ||
        snapshot.readOnly === true
      ) {
        const message = "That provider and model are not available for Verify.";
        setError(message);
        throw new Error(message);
      }
      setSelected({ identity, providerId, modelId });
      setError(null);
      setAnnouncement(`Verify will use ${provider.label} · ${model.label}.`);
    },
    [identity, snapshot, source?.status],
  );

  return useMemo(() => {
    if (source === undefined) return undefined;
    return {
      snapshot,
      status: source.status,
      selecting: false,
      error: error ?? source.error,
      announcement,
      currentAvailable: isCurrentExecutionAvailable(snapshot),
      select,
      retry: source.retry,
    };
  }, [announcement, error, select, snapshot, source]);
}
