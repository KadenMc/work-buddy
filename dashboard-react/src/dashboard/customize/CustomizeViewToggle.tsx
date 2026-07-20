import { Layout } from "@phosphor-icons/react/Layout";

import { Button } from "../../ui/Button";
import { useMediaQuery } from "../../hooks/useMediaQuery";
import { HelpTarget } from "../help";
import { useCustomizeMode } from "./CustomizeModeController";

/**
 * The cross-view "Customize view" entry control, mounted in the app-shell navbar. Pressing it
 * opens the current view's in-view layout editor through the shared CustomizeModeController, so
 * one navbar control drives customization on every standard-grid view. Routes that never
 * register a host (single-surface, settings) leave it disabled with no per-route plumbing.
 *
 * Entry only. Exit stays in the in-view toolbar, where Cancel and Done keep the dirty gating,
 * so this control disables itself for the duration of an open session. It intentionally does
 * not announce. The host's beginCustomize already announces the session start, and keeping a
 * single announcement source avoids a duplicate utterance.
 */
export function CustomizeViewToggle() {
  const { available, customizing, begin } = useCustomizeMode();
  // Self-gate on narrow, hover-less viewports where the desktop layout editor does not apply,
  // mirroring the prior grid-only control that CSS hid on mobile.
  const isMobile = useMediaQuery("(max-width: 767px)");
  return (
    <HelpTarget
      content={{
        summary: "Rearrange and resize the widgets in this view.",
        details:
          "Customize view opens a dedicated desktop layout editor. You can move, resize, add, hide, or remove eligible widgets, preview the result safely, and then save or cancel the entire layout change.",
      }}
      placement="bottom end"
      reactAriaComposite
    >
      <Button
        size="small"
        variant={customizing ? "primary" : "secondary"}
        aria-pressed={customizing}
        disabled={isMobile || !available || customizing}
        onClick={begin}
      >
        <Layout aria-hidden="true" /> Customize view
      </Button>
    </HelpTarget>
  );
}

export default CustomizeViewToggle;
