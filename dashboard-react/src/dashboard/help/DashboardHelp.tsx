import {
  cloneElement,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  type ReactElement,
  type ReactNode,
  type Ref,
} from "react";
import { mergeProps, mergeRefs, useTooltipTrigger } from "react-aria";
import {
  Tooltip,
  TooltipContext,
  TooltipTriggerStateContext,
  type TooltipProps,
} from "react-aria-components";
import { useTooltipTriggerState } from "react-stately";

import type { HelpContent } from "./contracts";

const DashboardHelpContext = createContext(false);
const HELP_OPEN_DELAY_MS = 700;
const HELP_CLOSE_DELAY_MS = 300;

export function DashboardHelpProvider({
  enabled,
  children,
}: {
  readonly enabled: boolean;
  readonly children: ReactNode;
}) {
  return (
    <DashboardHelpContext.Provider value={enabled}>
      {children}
    </DashboardHelpContext.Provider>
  );
}

export const useDashboardHelpEnabled = (): boolean =>
  useContext(DashboardHelpContext);

export function HelpTarget({
  content,
  children,
  placement = "top",
  focusable = false,
  reactAriaComposite = false,
  ariaLabel,
}: {
  readonly content?: HelpContent;
  readonly children: ReactElement<Record<string, unknown>>;
  readonly placement?: TooltipProps["placement"];
  /** Makes a normally static target, such as a widget title, keyboard reachable in Help mode. */
  readonly focusable?: boolean;
  /** Bridges React Aria composites that expose hover/focus behavior above their DOM root. */
  readonly reactAriaComposite?: boolean;
  readonly ariaLabel?: string;
}) {
  const enabled = useDashboardHelpEnabled() && content !== undefined;
  const state = useTooltipTriggerState({
    isDisabled: !enabled,
    delay: 0,
    closeDelay: HELP_CLOSE_DELAY_MS,
    shouldCloseOnPress: true,
  });
  const hoverTimerRef = useRef<number | undefined>(undefined);
  const clearHoverTimer = useCallback(() => {
    if (hoverTimerRef.current === undefined) return;
    window.clearTimeout(hoverTimerRef.current);
    hoverTimerRef.current = undefined;
  }, []);
  const openHelp = useCallback(
    (immediate?: boolean) => {
      clearHoverTimer();
      if (immediate) {
        state.open(true);
        return;
      }
      hoverTimerRef.current = window.setTimeout(() => {
        hoverTimerRef.current = undefined;
        state.open(true);
      }, HELP_OPEN_DELAY_MS);
    },
    [clearHoverTimer, state],
  );
  const closeHelp = useCallback(
    (immediate?: boolean) => {
      clearHoverTimer();
      state.close(immediate);
    },
    [clearHoverTimer, state],
  );
  const triggerState = { ...state, open: openHelp, close: closeHelp };
  useEffect(() => clearHoverTimer, [clearHoverTimer]);
  const triggerRef = useRef<HTMLElement>(null);
  const { triggerProps, tooltipProps } = useTooltipTrigger(
    {
      isDisabled: !enabled,
      delay: 0,
      closeDelay: HELP_CLOSE_DELAY_MS,
      shouldCloseOnPress: true,
    },
    triggerState,
    triggerRef,
  );

  if (!enabled || content === undefined) return children;

  const existingClassName =
    typeof children.props.className === "string" ? children.props.className : "";
  const existingRef = children.props.ref as Ref<HTMLElement> | undefined;
  const trigger = cloneElement(
    children,
    mergeProps(children.props, triggerProps, {
      className: `${existingClassName} wb-help-target`.trim(),
      "data-help-target": "true",
      ...(focusable ? { tabIndex: 0 } : {}),
      ...(ariaLabel ? { "aria-label": ariaLabel } : {}),
      ...(reactAriaComposite
        ? {
            onHoverStart: () => openHelp(),
            onHoverEnd: () => closeHelp(),
            onFocus: () => openHelp(true),
            onBlur: () => closeHelp(true),
            onPressStart: () => closeHelp(true),
          }
        : {}),
      // Avoid a competing browser-native title tooltip while contextual help is active.
      ...(children.props.title !== undefined ? { title: "" } : {}),
      ref: mergeRefs(existingRef, triggerRef),
    }),
  );

  return (
    <TooltipTriggerStateContext.Provider value={triggerState}>
      <TooltipContext.Provider value={{ ...tooltipProps, triggerRef }}>
        {trigger}
        <Tooltip
          className="wb-contextual-help"
          placement={placement}
          offset={8}
          onPointerEnter={() => openHelp(true)}
          onPointerLeave={() => closeHelp()}
        >
          <strong className="wb-contextual-help__summary">{content.summary}</strong>
          <span className="wb-contextual-help__details">{content.details}</span>
        </Tooltip>
      </TooltipContext.Provider>
    </TooltipTriggerStateContext.Provider>
  );
}
