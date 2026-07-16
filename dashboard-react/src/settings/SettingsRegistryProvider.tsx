import { createContext, useContext, type ReactNode } from "react";

import type { SettingsPageId } from "../dashboard/contributions/contracts";
import { nativeSettingsRegistry } from "./nativeContributions";
import type { SettingsRegistryState } from "./useSettingsRegistry";
import { useSettingsRegistry } from "./useSettingsRegistry";

const NATIVE_STATE: SettingsRegistryState = {
  registry: nativeSettingsRegistry,
  revision: "native",
  status: "native",
};

const SettingsRegistryContext = createContext<SettingsRegistryState | undefined>(
  undefined,
);

export function SettingsRegistryProvider({
  children,
}: {
  readonly children: ReactNode;
}) {
  const state = useSettingsRegistry();
  return (
    <SettingsRegistryContext.Provider value={state}>
      {children}
    </SettingsRegistryContext.Provider>
  );
}

/** Native fallback keeps isolated component/test renderers inert and deterministic. */
export function useSettingsCatalog(): SettingsRegistryState {
  return useContext(SettingsRegistryContext) ?? NATIVE_STATE;
}

export function useSettingsPageRoute(
  pageId: SettingsPageId | undefined,
): string | undefined {
  const { registry } = useSettingsCatalog();
  return pageId === undefined ? undefined : registry.getPage(pageId)?.route;
}
