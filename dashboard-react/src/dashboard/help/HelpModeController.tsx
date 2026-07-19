import {
  createContext,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { DashboardHelpProvider } from "./DashboardHelp";

/** The shared on/off controller for cross-view hover help. */
export interface HelpModeController {
  readonly enabled: boolean;
  setEnabled(value: boolean): void;
  toggle(): void;
}

const DISABLED_CONTROLLER: HelpModeController = {
  enabled: false,
  setEnabled: () => {},
  toggle: () => {},
};

const HelpModeControllerContext = createContext<HelpModeController | null>(null);

/**
 * App-shell owner of hover-help state. It lives above the navbar and the view outlet so
 * a single toggle in the header drives contextual help across every view, not only the
 * widget grid. It also feeds the boolean `DashboardHelpProvider` context every
 * `HelpTarget` reads, so the toggle and the targets stay in lockstep from one source.
 */
export function HelpModeProvider({ children }: { readonly children: ReactNode }) {
  const [enabled, setEnabled] = useState(false);
  const controller = useMemo<HelpModeController>(
    () => ({
      enabled,
      setEnabled,
      toggle: () => setEnabled((current) => !current),
    }),
    [enabled],
  );
  return (
    <HelpModeControllerContext.Provider value={controller}>
      <DashboardHelpProvider enabled={enabled}>{children}</DashboardHelpProvider>
    </HelpModeControllerContext.Provider>
  );
}

/**
 * Read the shared hover-help controller. When no provider is mounted (a surface rendered
 * outside the app shell, an isolated test, a standalone harness) this returns a stable
 * disabled no-op controller, so help is simply off rather than a crash.
 */
export function useHelpMode(): HelpModeController {
  return useContext(HelpModeControllerContext) ?? DISABLED_CONTROLLER;
}
