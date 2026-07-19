import { Info } from "@phosphor-icons/react/Info";

import { Button } from "../../ui/Button";
import { useMediaQuery } from "../../hooks/useMediaQuery";
import { useDashboardAnnouncer } from "../accessibility/DashboardAnnouncer";
import { useHelpMode } from "./HelpModeController";

/**
 * The cross-view hover-help toggle, mounted in the app-shell navbar. Pressing it flips
 * the shared help mode so every view's `HelpTarget`s reveal on hover. Disabled on narrow,
 * hover-less viewports where there is nothing to hover, mirroring the prior grid-only
 * control it replaces.
 */
export function HelpModeToggle() {
  const { enabled, setEnabled } = useHelpMode();
  const { announce } = useDashboardAnnouncer();
  const isMobile = useMediaQuery("(max-width: 767px)");
  return (
    <Button
      size="small"
      variant={enabled ? "primary" : "secondary"}
      aria-pressed={enabled}
      disabled={isMobile}
      onClick={() => {
        announce(enabled ? "Hover help turned off" : "Hover help turned on");
        setEnabled(!enabled);
      }}
    >
      <Info weight="duotone" aria-hidden="true" /> Hover help
    </Button>
  );
}

export default HelpModeToggle;
