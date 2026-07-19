/**
 * Dashboard-citizenship proof (PRD I18) for the two environment signals jsdom
 * cannot evaluate as CSS: forced-colors and reduced-motion. The proof drives each
 * signal through a controllable matchMedia and reads it back off the shared theme
 * runtime, so the surface is shown to OBSERVE the environment. For forced-colors
 * it then asserts the redundant non-colour encoding the CSS relies on: every
 * trust, drift, kind, and status state names itself in text and on a data
 * attribute, so meaning survives when the palette is replaced by system colours
 * (SP-6 G3, C1 section 5.4). The pixel-level rendering under the real @media
 * blocks is proven in the browser e2e specs, not in jsdom.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider, useTheme } from "../../../theme/ThemeProvider";
import { expectNoAccessibilityViolations } from "../../../test/setup";
import {
  CoworkRail,
  InMemoryReviewProvider,
  createDemoChatProvider,
} from "../rail";
import {
  FORCED_COLORS_QUERY,
  REDUCED_MOTION_QUERY,
  installMatchMedia,
  resetMedia,
  setMedia,
} from "./testMedia";

const S1_TLDR = "Add the vault content hash to the cache key.";

class MemoryStorage implements Storage {
  private map = new Map<string, string>();
  get length(): number {
    return this.map.size;
  }
  clear(): void {
    this.map.clear();
  }
  getItem(key: string): string | null {
    return this.map.get(key) ?? null;
  }
  key(index: number): string | null {
    return [...this.map.keys()][index] ?? null;
  }
  removeItem(key: string): void {
    this.map.delete(key);
  }
  setItem(key: string, value: string): void {
    this.map.set(key, value);
  }
}

function AccessibilityProbe() {
  const { theme } = useTheme();
  return (
    <output data-testid="a11y-probe">
      {`forced-colors:${theme.accessibility.forcedColors} reduced-motion:${theme.accessibility.reducedMotion}`}
    </output>
  );
}

function renderRail() {
  return render(
    <ThemeProvider initialPreference={{ scheme: "light", skinId: "wb.default" }}>
      <AccessibilityProbe />
      <CoworkRail
        documentId="demo-doc"
        reviewProvider={new InMemoryReviewProvider()}
        chatProvider={createDemoChatProvider("conv-env")}
        conversationId="conv-env"
        storage={new MemoryStorage()}
      />
    </ThemeProvider>,
  );
}

describe("Co-work environment modes", () => {
  beforeEach(() => {
    installMatchMedia();
  });
  afterEach(() => {
    resetMedia();
    vi.unstubAllGlobals();
  });

  it("observes forced-colors and keeps a non-colour encoding for every state", async () => {
    setMedia(FORCED_COLORS_QUERY, true);
    const { container } = renderRail();
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());

    // The surface observes the forced-colors environment through the theme hook.
    expect(screen.getByTestId("a11y-probe")).toHaveTextContent(
      "forced-colors:true",
    );

    // Drift: a text label rides alongside the data attribute.
    const drift = container.querySelector("[data-drift]");
    expect(drift).not.toBeNull();
    expect(drift?.textContent?.trim().length ?? 0).toBeGreaterThan(0);
    expect(screen.getByText(/In sync, no drift/)).toBeVisible();

    // Kind: the insertion card names its type in text, not only by colour.
    const kinded = container.querySelector('.wb-cowork-rail__card[data-kind]');
    expect(kinded).not.toBeNull();
    expect(screen.getAllByText(/Insertion|Deletion|Flag/).length).toBeGreaterThan(
      0,
    );

    // Claim status: the confirmed claim carries a text label and a data attribute.
    const status = container.querySelector("[data-status]");
    expect(status).not.toBeNull();
    expect(screen.getByText("Confirmed")).toBeVisible();

    await expectNoAccessibilityViolations(container);
  });

  it("observes reduced-motion and stays accessible", async () => {
    setMedia(REDUCED_MOTION_QUERY, true);
    const { container } = renderRail();
    await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());

    expect(screen.getByTestId("a11y-probe")).toHaveTextContent(
      "reduced-motion:true",
    );
    await expectNoAccessibilityViolations(container);
  });
});
