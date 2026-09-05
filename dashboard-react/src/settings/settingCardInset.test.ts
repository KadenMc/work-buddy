import { describe, expect, it } from "vitest";

import settingsStyles from "./styles.css?raw";

/**
 * A settings card declares no padding of its own, and neither does the control
 * wrapper inside it. Every control that fills that slot therefore carries its
 * own inset, or it renders flush against the card edge while the heading above
 * it stays indented.
 */
const CONTROLS_THAT_FILL_A_SETTING_CARD = [
  "wb-time-setting-control",
  "wb-select-setting-control",
  "wb-execution-profile-setting-control",
  "wb-keybinding-map",
];

const ruleFor = (className: string): string => {
  const start = settingsStyles.indexOf(`.${className} {`);
  expect(start, `no rule for .${className}`).toBeGreaterThan(-1);
  return settingsStyles.slice(start, settingsStyles.indexOf("}", start));
};

describe("settings card inset", () => {
  it("neither the card nor its control slot supplies the inset", () => {
    expect(ruleFor("wb-settings-card")).not.toContain("padding");
    expect(ruleFor("wb-settings-card__control")).not.toContain("padding");
  });

  it.each(CONTROLS_THAT_FILL_A_SETTING_CARD)(
    "%s carries its own inset from the shared spacing token",
    (className) => {
      expect(ruleFor(className)).toContain("padding: var(--wb-space-7)");
    },
  );
});
