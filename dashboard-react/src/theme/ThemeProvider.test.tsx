import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider, useTheme } from "./ThemeProvider";

interface MediaController {
  readonly query: string;
  matches: boolean;
  readonly listeners: Set<(event: MediaQueryListEvent) => void>;
}

const media = new Map<string, MediaController>();

function installMatchMedia(): void {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => {
      const controller =
        media.get(query) ??
        ({ query, matches: false, listeners: new Set() } satisfies MediaController);
      media.set(query, controller);
      return {
        media: query,
        get matches() {
          return controller.matches;
        },
        onchange: null,
        addEventListener: (
          _type: "change",
          listener: (event: MediaQueryListEvent) => void,
        ) => controller.listeners.add(listener),
        removeEventListener: (
          _type: "change",
          listener: (event: MediaQueryListEvent) => void,
        ) => controller.listeners.delete(listener),
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => true,
      };
    }),
  );
}

function changeMedia(query: string, matches: boolean): void {
  const controller = media.get(query)!;
  controller.matches = matches;
  controller.listeners.forEach((listener) =>
    listener({ matches, media: query } as MediaQueryListEvent),
  );
}

let rollbackPreview: (() => void) | undefined;

function ThemeProbe() {
  const runtime = useTheme();
  return (
    <div>
      <output>{`${runtime.theme.preference.skinId}:${runtime.theme.resolvedScheme}`}</output>
      <button
        type="button"
        onClick={() => runtime.setPreference({ scheme: "dark" })}
      >
        Dark
      </button>
      <button
        type="button"
        onClick={() => {
          rollbackPreview = runtime.beginPreview({
            scheme: "light",
            skinId: "wb.conformance-stress",
          });
        }}
      >
        Preview
      </button>
      <button type="button" onClick={() => rollbackPreview?.()}>
        Roll back
      </button>
    </div>
  );
}

describe("ThemeProvider", () => {
  beforeEach(() => {
    media.clear();
    localStorage.clear();
    rollbackPreview = undefined;
    installMatchMedia();
  });

  it("tracks system scheme only for a system preference", async () => {
    render(
      <ThemeProvider initialPreference={{ scheme: "system", skinId: "wb.default" }}>
        <ThemeProbe />
      </ThemeProvider>,
    );
    expect(screen.getByText("wb.default:light")).toBeInTheDocument();

    act(() => changeMedia("(prefers-color-scheme: dark)", true));
    expect(screen.getByText("wb.default:dark")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Dark" }));
    expect(screen.getByText("wb.default:dark")).toBeInTheDocument();
    expect(document.documentElement.dataset.wbScheme).toBe("dark");

    changeMedia("(prefers-color-scheme: dark)", false);
    expect(screen.getByText("wb.default:dark")).toBeInTheDocument();
    await waitFor(() =>
      expect(localStorage.getItem("wb.theme.preference.v1")).toContain('"dark"'),
    );
  });

  it("previews a validated alternate skin without persisting it and rolls back", () => {
    render(
      <ThemeProvider initialPreference={{ scheme: "dark", skinId: "wb.default" }}>
        <ThemeProbe />
      </ThemeProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    expect(screen.getByText("wb.conformance-stress:light")).toBeInTheDocument();
    expect(document.documentElement.dataset.wbSkin).toBe("wb.conformance-stress");
    expect(localStorage.getItem("wb.theme.preference.v1")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Roll back" }));
    expect(screen.getByText("wb.default:dark")).toBeInTheDocument();
    expect(document.documentElement.dataset.wbSkin).toBe("wb.default");
  });
});
