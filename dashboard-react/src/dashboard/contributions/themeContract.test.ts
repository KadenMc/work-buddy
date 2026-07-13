import { describe, expect, it } from "vitest";

import type { WidgetThemeDeclaration } from "./themeContract";
import { validateWidgetThemeDeclaration } from "./validate";

const standardTheme = (): WidgetThemeDeclaration => ({
  contractVersion: 1,
  conformance: "standard",
  supports: ["light", "dark", "forced-colors", "reduced-motion"],
  styling: "host-primitives",
});

describe("WidgetThemeDeclaration", () => {
  it("accepts the complete standard Theme Contract v1 matrix", () => {
    expect(validateWidgetThemeDeclaration(standardTheme())).toEqual([]);
  });

  it("rejects standard widgets that omit a scheme or accessibility mode", () => {
    const declaration: WidgetThemeDeclaration = {
      ...standardTheme(),
      supports: ["light", "forced-colors"],
    };

    expect(validateWidgetThemeDeclaration(declaration).map((issue) => issue.message)).toEqual(
      expect.arrayContaining([
        "standard widgets must support dark",
        "standard widgets must support reduced-motion",
      ]),
    );
  });

  it("requires a reason for each narrowly allowed visual exception", () => {
    const declaration: WidgetThemeDeclaration = {
      ...standardTheme(),
      exceptions: [{ kind: "fixed-brand-color", reason: "   " }],
    };

    expect(validateWidgetThemeDeclaration(declaration)).toEqual([
      expect.objectContaining({ code: "missing_theme_exception_reason" }),
    ]);
  });
});

