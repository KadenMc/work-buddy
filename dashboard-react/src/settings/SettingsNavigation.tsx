import { GearSix } from "@phosphor-icons/react/GearSix";
import { useLocation, useNavigate } from "react-router-dom";

import { IconButton } from "../ui";

export const SETTINGS_ACCESSIBILITY_PATH = "/settings/system/accessibility";

export interface SettingsNavigationState {
  readonly settingsReturnTo?: string;
  readonly settingsReturnLabel?: string;
}

export function isSafeDashboardReturnPath(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.startsWith("/") &&
    !value.startsWith("//") &&
    !value.startsWith("/settings")
  );
}

export function createSettingsNavigationState(
  currentPath: string,
  defaultViewPath: string,
  returnLabel?: string,
): SettingsNavigationState {
  return {
    settingsReturnTo: isSafeDashboardReturnPath(currentPath)
      ? currentPath
      : defaultViewPath,
    ...(returnLabel ? { settingsReturnLabel: returnLabel } : {}),
  };
}

export function resolveSettingsReturnLabel(
  state: unknown,
  fallback = "Back to dashboard",
): string {
  const candidate = (state as SettingsNavigationState | null)?.settingsReturnLabel;
  return typeof candidate === "string" && candidate.trim().length > 0
    ? candidate
    : fallback;
}

export function resolveSettingsReturnPath(
  state: unknown,
  defaultViewPath: string,
): string {
  const candidate = (state as SettingsNavigationState | null)?.settingsReturnTo;
  return isSafeDashboardReturnPath(candidate) ? candidate : defaultViewPath;
}

export function SettingsLauncher({
  defaultViewPath,
}: {
  readonly defaultViewPath: string;
}) {
  const location = useLocation();
  const navigate = useNavigate();
  const isSettings = location.pathname.startsWith("/settings");
  const returnPath = resolveSettingsReturnPath(location.state, defaultViewPath);

  return (
    <IconButton
      label={isSettings ? "Close settings" : "Open settings"}
      icon={<GearSix weight="duotone" />}
      variant="secondary"
      size="small"
      className={`wb-settings-trigger${isSettings ? " is-active" : ""}`}
      aria-pressed={isSettings}
      onClick={() => {
        if (isSettings) {
          navigate(returnPath);
          return;
        }
        const currentPath = `${location.pathname}${location.search}${location.hash}`;
        navigate(SETTINGS_ACCESSIBILITY_PATH, {
          state: createSettingsNavigationState(currentPath, defaultViewPath),
        });
      }}
    />
  );
}
