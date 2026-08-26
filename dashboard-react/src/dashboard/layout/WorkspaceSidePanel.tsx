import {
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  type ReactNode,
  type Ref,
} from "react";
import {
  Group,
  Panel,
  Separator,
  useDefaultLayout,
  type GroupImperativeHandle,
  type Layout,
  type LayoutChangedMeta,
  type LayoutStorage,
  type SeparatorProps,
} from "react-resizable-panels";

import { HelpTarget, type HelpContent } from "../help";
import "./WorkspaceSidePanel.css";

export type WorkspaceSidePanelMode =
  | "split"
  | "primary-only"
  | "side-only"
  | "stacked";

export interface WorkspaceSidePanelProps {
  /** Presentation preference only: never include draft or conversation contents. */
  readonly layoutId: string;
  readonly primaryId?: string;
  readonly sideId?: string;
  readonly mode: WorkspaceSidePanelMode;
  readonly primary: ReactNode;
  readonly side: ReactNode;
  readonly resizeLabel: string;
  readonly resizeHelp?: HelpContent;
  readonly primaryDefaultSize?: string;
  readonly primaryMinSize?: string;
  readonly sideDefaultSize?: string;
  readonly sideMinSize?: string;
  readonly sideMaxSize?: string;
  readonly className?: string;
  readonly primaryClassName?: string;
  readonly sideClassName?: string;
}

const RESIZE_HELP: HelpContent = {
  summary: "Resize the side panel.",
  details:
    "Drag this divider, or focus it and use the Left and Right arrow keys. Double-click to restore the default widths.",
};

// Always supply a storage object: the library's undefined fallback reads the
// global localStorage directly. Browser privacy settings, quota failures, and
// malformed preferences must not stop the workspace from opening.
const layoutStorage: LayoutStorage = {
  getItem(key) {
    try {
      if (typeof window === "undefined") return null;
      const stored = window.localStorage.getItem(key);
      if (stored === null) return null;
      const value: unknown = JSON.parse(stored);
      return value !== null && typeof value === "object" && !Array.isArray(value)
        ? stored
        : null;
    } catch {
      return null;
    }
  },
  setItem(key, value) {
    try {
      if (typeof window !== "undefined") window.localStorage.setItem(key, value);
    } catch {
      // Resizing still works for this mounted workspace without persistence.
    }
  },
};

function isSplitLayout(
  layout: Layout | undefined,
  primaryId: string,
  sideId: string,
): layout is Layout {
  if (layout === undefined || Object.keys(layout).length !== 2) return false;
  const primary = layout[primaryId];
  const side = layout[sideId];
  return (
    Number.isFinite(primary) &&
    Number.isFinite(side) &&
    primary >= 0 &&
    side >= 0 &&
    primary <= 100 &&
    side <= 100 &&
    Math.abs(primary + side - 100) < 0.01
  );
}

function percentageDefaults(
  primaryId: string,
  sideId: string,
  primarySize: string,
  sideSize: string,
): Layout | undefined {
  // Unitless library sizes are percentages too. Other units need the library's
  // first measured split; never reinterpret a pixel/rem default as a percentage.
  if (![primarySize, sideSize].every((size) => /^\d+(?:\.\d+)?%?$/.test(size.trim()))) {
    return undefined;
  }
  const layout = {
    [primaryId]: Number.parseFloat(primarySize),
    [sideId]: Number.parseFloat(sideSize),
  };
  return isSplitLayout(layout, primaryId, sideId) ? layout : undefined;
}

function sameSplit(left: Layout, right: Layout): boolean {
  return Object.keys(left).every((id) => Math.abs(left[id] - right[id]) < 0.001);
}

/**
 * HelpTarget clones its child with a DOM ref and focus handlers. Separator uses
 * elementRef and owns its bubble-phase focus handlers, so bridge help through
 * capture without replacing the library's focus/keyboard/pointer behavior.
 * There is no wrapper element: the separator stays a direct child of Group.
 */
function HelpableSeparator({
  ref,
  onFocus,
  onBlur,
  onFocusCapture,
  onBlurCapture,
  ...props
}: Omit<SeparatorProps, "elementRef"> & { readonly ref?: Ref<HTMLDivElement> }) {
  return (
    <Separator
      {...props}
      elementRef={ref}
      onFocusCapture={(event) => {
        onFocusCapture?.(event);
        onFocus?.(event);
      }}
      onBlurCapture={(event) => {
        onBlurCapture?.(event);
        onBlur?.(event);
      }}
    />
  );
}

/**
 * A workspace-owned horizontal split. Callers supply its height, responsive
 * policy, and content scroll boundaries; this primitive knows no viewport or
 * domain state. Both panel subtrees remain mounted in every presentation mode.
 */
export function WorkspaceSidePanel({
  layoutId,
  primaryId = "primary",
  sideId = "side",
  mode,
  primary,
  side,
  resizeLabel,
  resizeHelp = RESIZE_HELP,
  primaryDefaultSize = "67%",
  primaryMinSize = "30%",
  sideDefaultSize = "33%",
  sideMinSize = "15%",
  sideMaxSize = "70%",
  className,
  primaryClassName,
  sideClassName,
}: WorkspaceSidePanelProps) {
  const { defaultLayout, onLayoutChanged: saveLayout } = useDefaultLayout({
    id: layoutId,
    storage: layoutStorage,
    onlySaveAfterUserInteractions: true,
  });
  const split = mode === "split";
  const layoutIdentity = JSON.stringify([layoutId, primaryId, sideId]);
  const initialLayout = useMemo(
    () => isSplitLayout(defaultLayout, primaryId, sideId)
      ? { ...defaultLayout }
      : percentageDefaults(primaryId, sideId, primaryDefaultSize, sideDefaultSize),
    [defaultLayout, primaryId, sideId, primaryDefaultSize, sideDefaultSize],
  );
  const intendedSplit = useRef({ identity: layoutIdentity, layout: initialLayout });
  const groupRef = useRef<GroupImperativeHandle | null>(null);
  const groupElementRef = useRef<HTMLDivElement | null>(null);
  const observedWidth = useRef<number | undefined>(undefined);
  const restoring = useRef(false);
  const restoreFrame = useRef<number | undefined>(undefined);
  const cancelRestore = useCallback(() => {
    if (restoreFrame.current !== undefined) {
      window.cancelAnimationFrame(restoreFrame.current);
      restoreFrame.current = undefined;
    }
  }, []);
  const restoreSplit = useCallback(() => {
    const intent = intendedSplit.current;
    const group = groupRef.current;
    if (!split || restoring.current || intent.identity !== layoutIdentity || !intent.layout || !group) return;
    const current = group.getLayout();
    if (!isSplitLayout(current, primaryId, sideId) || sameSplit(current, intent.layout)) return;
    restoring.current = true;
    try {
      // The library may clamp this while the container is narrow. Keep the
      // intention unchanged so a later measurement can restore it when it fits.
      group.setLayout(intent.layout);
    } finally {
      restoring.current = false;
    }
  }, [layoutIdentity, primaryId, sideId, split]);
  const scheduleRestore = useCallback(() => {
    if (restoreFrame.current !== undefined || restoring.current) return;
    // Finish the library's measurement/event pass before reconciling. In
    // particular, its double-click reset is reported as an imperative change;
    // the actual separator event below must first adopt that new user intent.
    restoreFrame.current = window.requestAnimationFrame(() => {
      restoreFrame.current = undefined;
      restoreSplit();
    });
  }, [restoreSplit]);
  const onPanelResize = useCallback(() => {
    // Group suppresses layout callbacks when only pixel size/constraints change.
    // Reuse the library's panel observer to retry once a clamped split can fit.
    // A user drag changes panel widths but not the container: never reconcile
    // those intermediate drag sizes against the last settled user preference.
    const width = groupElementRef.current?.offsetWidth;
    const changed = width !== observedWidth.current;
    observedWidth.current = width;
    if (split && width && changed) scheduleRestore();
  }, [scheduleRestore, split]);
  useLayoutEffect(() => {
    if (intendedSplit.current.identity !== layoutIdentity) {
      intendedSplit.current = { identity: layoutIdentity, layout: initialLayout };
    }
    restoreSplit();
    return cancelRestore;
  }, [cancelRestore, initialLayout, layoutIdentity, restoreSplit]);
  const onLayoutChanged = useCallback(
    (layout: Layout, meta: LayoutChangedMeta) => {
      const intent = intendedSplit.current;
      if (!split || intent.identity !== layoutIdentity || !isSplitLayout(layout, primaryId, sideId)) return;
      if (meta.isUserInteraction) {
        cancelRestore();
        intent.layout = { ...layout };
        saveLayout(layout, meta);
      } else if (intent.layout === undefined) {
        intent.layout = { ...layout };
      } else if (!sameSplit(layout, intent.layout)) {
        // Disabled/hidden panels still get clamped by the library's observer.
        // Neither those changes nor restoration may replace or persist intent.
        scheduleRestore();
      }
    },
    [cancelRestore, layoutIdentity, primaryId, sideId, saveLayout, scheduleRestore, split],
  );
  const adoptDoubleClickReset = useCallback(() => {
    const layout = groupRef.current?.getLayout();
    if (!split || intendedSplit.current.identity !== layoutIdentity || !isSplitLayout(layout, primaryId, sideId)) return;
    cancelRestore();
    intendedSplit.current.layout = { ...layout };
    saveLayout(layout, { isUserInteraction: true });
  }, [cancelRestore, layoutIdentity, primaryId, sideId, saveLayout, split]);
  const primaryHidden = mode === "side-only";
  const sideHidden = mode === "primary-only";

  return (
    <Group
      className={["wb-workspace-side-panel", className].filter(Boolean).join(" ")}
      data-workspace-panel-mode={mode}
      orientation="horizontal"
      groupRef={groupRef}
      elementRef={groupElementRef}
      disabled={!split}
      defaultLayout={
        isSplitLayout(defaultLayout, primaryId, sideId) ? defaultLayout : undefined
      }
      onLayoutChanged={onLayoutChanged}
    >
      <Panel
        id={primaryId}
        className={["wb-workspace-side-panel__panel", primaryClassName]
          .filter(Boolean)
          .join(" ")}
        data-workspace-pane="primary"
        defaultSize={primaryDefaultSize}
        minSize={primaryMinSize}
        onResize={onPanelResize}
        hidden={primaryHidden}
        inert={primaryHidden ? true : undefined}
      >
        {primary}
      </Panel>
      <HelpTarget content={split ? resizeHelp : undefined}>
        <HelpableSeparator
          className="wb-workspace-side-panel__separator"
          aria-label={resizeLabel}
          hidden={!split}
          disabled={!split}
          onDoubleClick={adoptDoubleClickReset}
        />
      </HelpTarget>
      <Panel
        id={sideId}
        className={["wb-workspace-side-panel__panel", sideClassName]
          .filter(Boolean)
          .join(" ")}
        data-workspace-pane="side"
        defaultSize={sideDefaultSize}
        minSize={sideMinSize}
        maxSize={sideMaxSize}
        onResize={onPanelResize}
        hidden={sideHidden}
        inert={sideHidden ? true : undefined}
      >
        {side}
      </Panel>
    </Group>
  );
}
