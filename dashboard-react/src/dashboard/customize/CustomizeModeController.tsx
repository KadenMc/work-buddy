import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

/**
 * A view host's contribution to the shared Customize control. The host owns the actual
 * layout-editing machinery, so it exposes only the one thing the navbar entry needs, a way
 * to open that machinery.
 */
export interface CustomizeModeHandle {
  begin(): void;
}

/**
 * The lease returned to a host when it registers. The host propagates its own customizing
 * state through `setCustomizing` and drops the lease through `unregister` on unmount.
 */
export interface CustomizeModeRegistration {
  setCustomizing(value: boolean): void;
  unregister(): void;
}

/** The shared, cross-view controller behind the navbar Customize entry control. */
export interface CustomizeModeController {
  /** True when some view host has registered, so the entry control has a target. */
  readonly available: boolean;
  /** True while the current host is running a customize session. */
  readonly customizing: boolean;
  /** Open the current host's customize machinery, or do nothing when none is registered. */
  begin(): void;
  /** Register a host as the current customize target. Last writer wins. */
  register(handle: CustomizeModeHandle): CustomizeModeRegistration;
}

const NOOP_REGISTRATION: CustomizeModeRegistration = {
  setCustomizing: () => {},
  unregister: () => {},
};

const DISABLED_CONTROLLER: CustomizeModeController = {
  available: false,
  customizing: false,
  begin: () => {},
  register: () => NOOP_REGISTRATION,
};

const CustomizeModeControllerContext =
  createContext<CustomizeModeController | null>(null);

/**
 * App-shell owner of Customize state. It lives above the navbar and the view outlet so a
 * single navbar entry control can open the current view's in-view layout editor. Only a
 * standard-grid view host registers, so single-surface and settings routes leave the control
 * inert with no per-route plumbing. Exit stays in the in-view toolbar, so this owner only ever
 * begins a session and tracks whether one is open.
 */
export function CustomizeModeProvider({
  children,
}: {
  readonly children: ReactNode;
}) {
  // The live handle is held in a ref so the registration closures below can compare identities
  // synchronously. Reading React state inside those closures would see a stale value. Two
  // pieces of state mirror what the entry control renders on, whether a host is present and
  // whether the current host is customizing, so the control re-renders when either changes.
  const handleRef = useRef<CustomizeModeHandle | null>(null);
  const [available, setAvailable] = useState(false);
  const [customizing, setCustomizing] = useState(false);

  const register = useCallback(
    (handle: CustomizeModeHandle): CustomizeModeRegistration => {
      // Last writer wins. A freshly mounted host takes ownership from any prior one, which
      // keeps route transitions correct even when the incoming host mounts before the outgoing
      // host unmounts. A new owner has not begun a session, so start from not-customizing.
      handleRef.current = handle;
      setAvailable(true);
      setCustomizing(false);
      return {
        setCustomizing: (value: boolean) => {
          // Only the current owner may drive the shared state, so a superseded host tearing
          // down cannot clobber the view that replaced it.
          if (handleRef.current !== handle) return;
          setCustomizing(value);
        },
        unregister: () => {
          // Unregister only if still current. An outgoing host that was already superseded
          // must not clear the incoming host's registration.
          if (handleRef.current !== handle) return;
          handleRef.current = null;
          setAvailable(false);
          setCustomizing(false);
        },
      };
    },
    [],
  );

  const begin = useCallback(() => {
    handleRef.current?.begin();
  }, []);

  const controller = useMemo<CustomizeModeController>(
    () => ({ available, customizing, begin, register }),
    [available, customizing, begin, register],
  );

  return (
    <CustomizeModeControllerContext.Provider value={controller}>
      {children}
    </CustomizeModeControllerContext.Provider>
  );
}

/**
 * Read the shared Customize controller. When no provider is mounted (a surface rendered
 * outside the app shell, an isolated test, a standalone harness) this returns a stable disabled
 * no-op controller, so the entry control is simply inert rather than a crash.
 */
export function useCustomizeMode(): CustomizeModeController {
  return useContext(CustomizeModeControllerContext) ?? DISABLED_CONTROLLER;
}
