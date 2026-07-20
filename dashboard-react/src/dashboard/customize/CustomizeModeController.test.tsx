import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  CustomizeModeProvider,
  useCustomizeMode,
  type CustomizeModeRegistration,
} from "./CustomizeModeController";

describe("useCustomizeMode without a provider", () => {
  it("returns a disabled controller whose operations are safe no-ops", () => {
    const { result } = renderHook(() => useCustomizeMode());

    expect(result.current.available).toBe(false);
    expect(result.current.customizing).toBe(false);
    expect(() => result.current.begin()).not.toThrow();

    const registration = result.current.register({ begin: () => {} });
    expect(() => registration.setCustomizing(true)).not.toThrow();
    expect(() => registration.unregister()).not.toThrow();
    // Registering against the fallback never enables it. There is no shell to drive.
    expect(result.current.available).toBe(false);
  });

  it("returns the same disabled controller reference across re-renders", () => {
    const { result, rerender } = renderHook(() => useCustomizeMode());
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
  });
});

describe("CustomizeModeProvider", () => {
  const wrapper = CustomizeModeProvider;

  it("becomes available when a host registers and inert when it unregisters", () => {
    const { result } = renderHook(() => useCustomizeMode(), { wrapper });
    expect(result.current.available).toBe(false);

    let registration!: CustomizeModeRegistration;
    act(() => {
      registration = result.current.register({ begin: () => {} });
    });
    expect(result.current.available).toBe(true);

    act(() => {
      registration.unregister();
    });
    expect(result.current.available).toBe(false);
  });

  it("routes begin to the most recently registered host (last writer wins)", () => {
    const { result } = renderHook(() => useCustomizeMode(), { wrapper });
    const first = vi.fn();
    const second = vi.fn();

    act(() => {
      result.current.register({ begin: first });
    });
    act(() => {
      result.current.register({ begin: second });
    });
    act(() => {
      result.current.begin();
    });

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("begin is a safe no-op while no host is registered", () => {
    const { result } = renderHook(() => useCustomizeMode(), { wrapper });
    expect(() =>
      act(() => {
        result.current.begin();
      }),
    ).not.toThrow();
  });

  it("ignores unregister from a registration that has been superseded", () => {
    const { result } = renderHook(() => useCustomizeMode(), { wrapper });

    let stale!: CustomizeModeRegistration;
    act(() => {
      stale = result.current.register({ begin: () => {} });
    });
    act(() => {
      result.current.register({ begin: () => {} });
    });

    act(() => {
      stale.unregister();
    });
    // The second registration is still current, so the control stays available.
    expect(result.current.available).toBe(true);
  });

  it("propagates customizing from the current registration", () => {
    const { result } = renderHook(() => useCustomizeMode(), { wrapper });

    let registration!: CustomizeModeRegistration;
    act(() => {
      registration = result.current.register({ begin: () => {} });
    });
    expect(result.current.customizing).toBe(false);

    act(() => {
      registration.setCustomizing(true);
    });
    expect(result.current.customizing).toBe(true);

    act(() => {
      registration.setCustomizing(false);
    });
    expect(result.current.customizing).toBe(false);
  });

  it("ignores customizing updates from a superseded registration", () => {
    const { result } = renderHook(() => useCustomizeMode(), { wrapper });

    let stale!: CustomizeModeRegistration;
    act(() => {
      stale = result.current.register({ begin: () => {} });
    });
    act(() => {
      result.current.register({ begin: () => {} });
    });

    act(() => {
      stale.setCustomizing(true);
    });
    expect(result.current.customizing).toBe(false);
  });
});
