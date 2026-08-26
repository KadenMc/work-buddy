import { useCallback, useEffect, useState } from "react";

import { useOptionalDashboardEvents } from "../dashboard/events/DashboardEventProvider";
import type {
  EffectiveSettingValue,
  SettingId,
  SettingsValueSnapshot,
} from "./contracts";
import {
  deleteSettingValue,
  fetchSettingsValues,
  patchSettingValue,
  SettingsServerError,
} from "./serverSettings";

export interface SettingsValuesState {
  readonly snapshot?: SettingsValueSnapshot;
  readonly status: "loading" | "ready" | "unavailable" | "error";
  readonly mutationSettingId?: SettingId;
  readonly message?: string;
  readonly error?: string;
  write(settingId: SettingId, value: unknown): Promise<void>;
  reset(settingId: SettingId): Promise<void>;
  /** Adopt a canonical mutation response returned by an embedded control. */
  adopt(value: EffectiveSettingValue): void;
}

function replaceValue(
  snapshot: SettingsValueSnapshot,
  value: EffectiveSettingValue,
): SettingsValueSnapshot {
  const current = snapshot.values.get(value.settingId);
  const currentMatch = current?.revision.match(/^(.*:)(\d+)$/);
  const nextMatch = value.revision.match(/^(.*:)(\d+)$/);
  if (
    currentMatch &&
    nextMatch &&
    currentMatch[1] === nextMatch[1] &&
    Number(nextMatch[2]) < Number(currentMatch[2])
  ) {
    return snapshot;
  }
  const values = new Map(snapshot.values);
  values.set(value.settingId, value);
  return { ...snapshot, values };
}

export function useSettingsValues(
  contextId: string,
  enabled = true,
): SettingsValuesState {
  const events = useOptionalDashboardEvents();
  const settingsInvalidationSequence =
    events?.lastInvalidation?.invalidation.reason === "settings.changed"
      ? events.lastInvalidation.sequence
      : 0;
  const reconcileSequence = events?.reconcileSignal?.sequence ?? 0;
  const [snapshot, setSnapshot] = useState<SettingsValueSnapshot>();
  const [status, setStatus] = useState<SettingsValuesState["status"]>("loading");
  const [mutationSettingId, setMutationSettingId] = useState<SettingId>();
  const [message, setMessage] = useState<string>();
  const [error, setError] = useState<string>();

  useEffect(() => {
    if (!enabled) {
      setSnapshot(undefined);
      setStatus("unavailable");
      setError(undefined);
      return;
    }
    const controller = new AbortController();
    setStatus("loading");
    setError(undefined);
    setMessage(undefined);
    void fetchSettingsValues(contextId, fetch, controller.signal)
      .then((next) => {
        if (!next) {
          setSnapshot(undefined);
          setStatus("unavailable");
          return;
        }
        setSnapshot((previous) => {
          if (!previous) return next;
          let reconciled = next;
          for (const value of previous.values.values()) {
            const previousRevision = value.revision.match(/^(.*:)(\d+)$/);
            const nextRevision = next.values.get(value.settingId)?.revision.match(/^(.*:)(\d+)$/);
            if (previousRevision && nextRevision && previousRevision[1] === nextRevision[1] && Number(previousRevision[2]) > Number(nextRevision[2])) {
              reconciled = replaceValue(reconciled, value);
            }
          }
          return reconciled;
        });
        setStatus("ready");
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setStatus("error");
        setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => controller.abort();
  }, [contextId, enabled, reconcileSequence, settingsInvalidationSequence]);

  const handleMutationError = useCallback(
    (reason: unknown) => {
      if (reason instanceof SettingsServerError && reason.authoritativeValue) {
        setSnapshot((current) =>
          current ? replaceValue(current, reason.authoritativeValue!) : current,
        );
      }
      setError(reason instanceof Error ? reason.message : String(reason));
      setMessage(undefined);
    },
    [],
  );

  const adopt = useCallback((value: EffectiveSettingValue) => {
    setSnapshot((previous) => previous ? replaceValue(previous, value) : previous);
  }, []);

  const write = useCallback(
    async (settingId: SettingId, value: unknown) => {
      const currentSnapshot = snapshot;
      if (!currentSnapshot) return;
      const current = currentSnapshot.values.get(settingId);
      if (!current || currentSnapshot.readOnly) return;
      setMutationSettingId(settingId);
      setError(undefined);
      setMessage(undefined);
      try {
        const result = await patchSettingValue(
          settingId,
          value,
          current.revision,
        );
        setSnapshot((previous) =>
          previous ? replaceValue(previous, result.value) : previous,
        );
        setMessage(
          result.value.pendingValue !== undefined
            ? "Saved. The change is pending."
            : "Setting saved.",
        );
      } catch (reason) {
        handleMutationError(reason);
      } finally {
        setMutationSettingId(undefined);
      }
    },
    [handleMutationError, snapshot],
  );

  const reset = useCallback(
    async (settingId: SettingId) => {
      const currentSnapshot = snapshot;
      if (!currentSnapshot) return;
      const current = currentSnapshot.values.get(settingId);
      if (!current || currentSnapshot.readOnly) return;
      setMutationSettingId(settingId);
      setError(undefined);
      setMessage(undefined);
      try {
        const result = await deleteSettingValue(settingId, current.revision);
        setSnapshot((previous) =>
          previous ? replaceValue(previous, result.value) : previous,
        );
        setMessage("Override reset to default.");
      } catch (reason) {
        handleMutationError(reason);
      } finally {
        setMutationSettingId(undefined);
      }
    },
    [handleMutationError, snapshot],
  );

  return {
    snapshot,
    status,
    mutationSettingId,
    message,
    error,
    write,
    reset,
    adopt,
  };
}
