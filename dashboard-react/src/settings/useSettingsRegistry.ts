import { useEffect, useState } from "react";

import { nativeSettingsRegistry } from "./nativeContributions";
import { SettingsRegistry } from "./registry";
import { fetchSettingsRegistry } from "./serverSettings";

export interface SettingsRegistryState {
  readonly registry: SettingsRegistry;
  readonly revision: string;
  readonly status: "native" | "loading" | "ready" | "degraded";
  readonly error?: string;
}

export function useSettingsRegistry(): SettingsRegistryState {
  const [state, setState] = useState<SettingsRegistryState>({
    registry: nativeSettingsRegistry,
    revision: "native",
    status: "loading",
  });

  useEffect(() => {
    const controller = new AbortController();
    void fetchSettingsRegistry(fetch, controller.signal)
      .then((result) => {
        if (!result) {
          setState({
            registry: nativeSettingsRegistry,
            revision: "native",
            status: "native",
          });
          return;
        }
        const registry = nativeSettingsRegistry.mergeAuthoritative(
          result.contribution,
        );
        setState({
          registry,
          revision: result.registryRevision,
          status: "ready",
        });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          registry: nativeSettingsRegistry,
          revision: "native",
          status: "degraded",
          error: error instanceof Error ? error.message : String(error),
        });
      });
    return () => controller.abort();
  }, []);

  return state;
}
