import { useEffect, useRef } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CustomizeModeProvider,
  useCustomizeMode,
  type CustomizeModeRegistration,
} from "./CustomizeModeController";
import { CustomizeViewToggle } from "./CustomizeViewToggle";

const media = (matches: boolean): MediaQueryList =>
  ({
    matches,
    media: "",
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(() => true),
  }) as unknown as MediaQueryList;

afterEach(() => vi.unstubAllGlobals());

/**
 * A stand-in for a standard-grid view host. It registers with the shared controller exactly the
 * way the real ViewHost will, with begin routed through a ref so the latest closure runs and its
 * own customizing state propagated on change. Keeping this in the test exercises the toggle
 * against the real registration lifecycle rather than a hand-mocked controller.
 */
function RegisterHost({
  begin,
  customizing = false,
}: {
  readonly begin: () => void;
  readonly customizing?: boolean;
}) {
  const { register } = useCustomizeMode();
  const beginRef = useRef(begin);
  beginRef.current = begin;
  const registrationRef = useRef<CustomizeModeRegistration | null>(null);
  useEffect(() => {
    const registration = register({ begin: () => beginRef.current() });
    registrationRef.current = registration;
    return () => {
      registration.unregister();
      registrationRef.current = null;
    };
  }, [register]);
  useEffect(() => {
    registrationRef.current?.setCustomizing(customizing);
  }, [customizing]);
  return null;
}

describe("CustomizeViewToggle", () => {
  it("is disabled when no view host is registered", () => {
    vi.stubGlobal("matchMedia", vi.fn(() => media(false)));
    render(
      <CustomizeModeProvider>
        <CustomizeViewToggle />
      </CustomizeModeProvider>,
    );
    const button = screen.getByRole("button", { name: "Customize view" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-pressed", "false");
  });

  it("stays disabled on narrow viewports even when a host is registered", () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => media(query === "(max-width: 767px)")),
    );
    render(
      <CustomizeModeProvider>
        <RegisterHost begin={() => {}} />
        <CustomizeViewToggle />
      </CustomizeModeProvider>,
    );
    expect(
      screen.getByRole("button", { name: "Customize view" }),
    ).toBeDisabled();
  });

  it("is disabled and pressed while a customize session is open", () => {
    vi.stubGlobal("matchMedia", vi.fn(() => media(false)));
    render(
      <CustomizeModeProvider>
        <RegisterHost begin={() => {}} customizing />
        <CustomizeViewToggle />
      </CustomizeModeProvider>,
    );
    const button = screen.getByRole("button", { name: "Customize view" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(button).toHaveClass("wb-button--primary");
  });

  it("is enabled when a host is registered and begins the session on click", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => media(false)));
    const begin = vi.fn();
    render(
      <CustomizeModeProvider>
        <RegisterHost begin={begin} />
        <CustomizeViewToggle />
      </CustomizeModeProvider>,
    );
    const button = screen.getByRole("button", { name: "Customize view" });
    expect(button).toBeEnabled();
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(button).toHaveClass("wb-button--secondary");

    await userEvent.click(button);
    expect(begin).toHaveBeenCalledTimes(1);
  });

  it("reflects the current customizing state via aria-pressed", () => {
    vi.stubGlobal("matchMedia", vi.fn(() => media(false)));
    const { rerender } = render(
      <CustomizeModeProvider>
        <RegisterHost begin={() => {}} customizing={false} />
        <CustomizeViewToggle />
      </CustomizeModeProvider>,
    );
    expect(
      screen.getByRole("button", { name: "Customize view" }),
    ).toHaveAttribute("aria-pressed", "false");

    rerender(
      <CustomizeModeProvider>
        <RegisterHost begin={() => {}} customizing={true} />
        <CustomizeViewToggle />
      </CustomizeModeProvider>,
    );
    expect(
      screen.getByRole("button", { name: "Customize view" }),
    ).toHaveAttribute("aria-pressed", "true");
  });
});
