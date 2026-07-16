import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import {
  DEFAULT_TYPOGRAPHY_SCALE,
  readTypographyScale,
  TYPOGRAPHY_SCALE_STORAGE_KEY,
  TypographyScaleProvider,
  useTypographyScale,
} from "./TypographyScaleProvider";

function Probe() {
  const { scale, setScale, resetScale } = useTypographyScale();
  return (
    <div>
      <output>{scale}</output>
      <button type="button" onClick={() => setScale("extra-large")}>
        Larger
      </button>
      <button type="button" onClick={resetScale}>
        Reset
      </button>
    </div>
  );
}

describe("TypographyScaleProvider", () => {
  beforeEach(() => {
    localStorage.clear();
    delete document.documentElement.dataset.wbTypeScale;
  });

  it("rejects unknown persisted values", () => {
    localStorage.setItem(TYPOGRAPHY_SCALE_STORAGE_KEY, "tiny");
    expect(readTypographyScale()).toBe(DEFAULT_TYPOGRAPHY_SCALE);
  });

  it("applies, persists, and resets the shared type scale", () => {
    render(
      <TypographyScaleProvider initialScale="standard">
        <Probe />
      </TypographyScaleProvider>,
    );

    expect(document.documentElement.dataset.wbTypeScale).toBe("standard");
    fireEvent.click(screen.getByRole("button", { name: "Larger" }));
    expect(screen.getByText("extra-large")).toBeInTheDocument();
    expect(document.documentElement.dataset.wbTypeScale).toBe("extra-large");
    expect(localStorage.getItem(TYPOGRAPHY_SCALE_STORAGE_KEY)).toBe(
      "extra-large",
    );

    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    expect(document.documentElement.dataset.wbTypeScale).toBe("standard");
    expect(localStorage.getItem(TYPOGRAPHY_SCALE_STORAGE_KEY)).toBe("standard");
  });
});
