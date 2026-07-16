import { describe, expect, it } from "vitest";

import {
  CALM_WORKSHOP_SKIN_ID,
  DEFAULT_SKIN_ID,
  getThemeSkin,
  listThemeSkins,
} from "./registry";

describe("theme skin registry", () => {
  it("keeps the orange product default and the prior Calm Workshop palette separately selectable", () => {
    expect(getThemeSkin(DEFAULT_SKIN_ID)).toMatchObject({
      label: "Default",
      purpose: "product",
      identity: { version: 2 },
    });
    expect(getThemeSkin(CALM_WORKSHOP_SKIN_ID)).toMatchObject({
      label: "Calm Workshop",
      purpose: "product",
    });
    expect(listThemeSkins().filter((skin) => skin.purpose === "product")).toHaveLength(3);
  });
});
