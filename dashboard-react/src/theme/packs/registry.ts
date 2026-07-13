import type { ThemeSkinIdentity } from "../contracts";

export const DEFAULT_SKIN_ID = "wb.default";
export const STUDIO_SKIN_ID = "wb.studio";
export const CONFORMANCE_STRESS_SKIN_ID = "wb.conformance-stress";

export interface ThemeSkinDefinition {
  readonly identity: ThemeSkinIdentity;
  readonly label: string;
  readonly description: string;
  readonly schemes: readonly ["light", "dark"];
  readonly purpose: "product" | "conformance-fixture";
}

const skins: Readonly<Record<string, ThemeSkinDefinition>> = Object.freeze({
  [DEFAULT_SKIN_ID]: {
    identity: {
      id: DEFAULT_SKIN_ID,
      version: 1,
      publisherAppId: "wb.core",
    },
    label: "Calm Workshop",
    description: "Warm ink, quiet sky, and focused ember accents.",
    schemes: ["light", "dark"],
    purpose: "product",
  },
  [STUDIO_SKIN_ID]: {
    identity: {
      id: STUDIO_SKIN_ID,
      version: 1,
      publisherAppId: "wb.core",
    },
    label: "Studio Slate",
    description: "A crisp, cool workspace with indigo accents.",
    schemes: ["light", "dark"],
    purpose: "product",
  },
  [CONFORMANCE_STRESS_SKIN_ID]: {
    identity: {
      id: CONFORMANCE_STRESS_SKIN_ID,
      version: 1,
      publisherAppId: "wb.core",
    },
    label: "Conformance Stress",
    description: "An intentionally adversarial developer test skin.",
    schemes: ["light", "dark"],
    purpose: "conformance-fixture",
  },
});

export const listThemeSkins = (): readonly ThemeSkinDefinition[] =>
  Object.values(skins);

export const isKnownThemeSkin = (skinId: string): boolean => skinId in skins;

export const getThemeSkin = (skinId: string): ThemeSkinDefinition =>
  skins[skinId] ?? skins[DEFAULT_SKIN_ID]!;
