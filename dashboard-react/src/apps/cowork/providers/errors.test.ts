import { describe, expect, it } from "vitest";

import { coworkErrorMessage } from "./errors";

describe("coworkErrorMessage", () => {
  it("keeps useful human guidance", () => {
    expect(
      coworkErrorMessage(
        { message: "Markdown changed outside Co-work; review it before saving." },
        "The file couldn’t be saved.",
      ),
    ).toBe("Markdown changed outside Co-work; review it before saving.");
  });

  it("keeps storage internals in diagnostics instead of UI copy", () => {
    expect(
      coworkErrorMessage(
        { message: "Canonical Y.Doc snapshot SHA256 did not match the structured head." },
        "This document couldn’t be opened.",
      ),
    ).toBe("This document couldn’t be opened.");
  });
});
