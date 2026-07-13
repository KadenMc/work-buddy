import type { ThemeSkinIdentity } from "../contracts";

export const DEFAULT_SKIN_ID = "wb.default";
export const CONFORMANCE_STRESS_SKIN_ID = "wb.conformance-stress";

export interface ThemeSkinDefinition {
  readonly identity: ThemeSkinIdentity;
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
    schemes: ["light", "dark"],
    purpose: "product",
  },
  [CONFORMANCE_STRESS_SKIN_ID]: {
    identity: {
      id: CONFORMANCE_STRESS_SKIN_ID,
      version: 1,
      publisherAppId: "wb.core",
    },
    schemes: ["light", "dark"],
    purpose: "conformance-fixture",
  },
});

export const listThemeSkins = (): readonly ThemeSkinDefinition[] =>
  Object.values(skins);

export const isKnownThemeSkin = (skinId: string): boolean => skinId in skins;

export const getThemeSkin = (skinId: string): ThemeSkinDefinition =>
  skins[skinId] ?? skins[DEFAULT_SKIN_ID]!;
