import { useEffect, useState } from "react";

import type {
  ProposedSettingPreview,
  SettingId,
} from "./contracts";
import { previewSettingValue } from "./serverSettings";

export interface SettingPreviewState {
  readonly status: "idle" | "waiting" | "loading" | "ready" | "error";
  readonly preview?: ProposedSettingPreview;
  readonly error?: string;
}

export function useSettingPreview({
  settingId,
  value,
  expectedRevision,
  enabled,
  debounceMs = 400,
}: {
  readonly settingId: SettingId;
  readonly value: unknown;
  readonly expectedRevision?: string;
  readonly enabled: boolean;
  readonly debounceMs?: number;
}): SettingPreviewState {
  const [state, setState] = useState<SettingPreviewState>({ status: "idle" });

  useEffect(() => {
    if (!enabled || !expectedRevision) {
      setState({ status: "idle" });
      return;
    }
    const controller = new AbortController();
    setState({ status: "waiting" });
    const timeout = window.setTimeout(() => {
      setState({ status: "loading" });
      void previewSettingValue(
        settingId,
        value,
        expectedRevision,
        fetch,
        controller.signal,
      )
        .then((preview) => {
          if (!controller.signal.aborted) {
            setState({ status: "ready", preview });
          }
        })
        .catch((reason: unknown) => {
          if (controller.signal.aborted) return;
          setState({
            status: "error",
            error: reason instanceof Error ? reason.message : String(reason),
          });
        });
    }, debounceMs);

    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [debounceMs, enabled, expectedRevision, settingId, value]);

  return state;
}
