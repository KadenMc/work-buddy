import { describe, expect, it } from "vitest";

import {
  DEFAULT_THEME_PREFERENCE,
  parseStoredThemePreference,
  readThemePreference,
  THEME_PREFERENCE_STORAGE_KEY,
  writeThemePreference,
} from "./preference";

class MemoryStorage implements Storage {
  readonly #values = new Map<string, string>();

  get length(): number {
    return this.#values.size;
  }

  clear(): void {
    this.#values.clear();
  }

  getItem(key: string): string | null {
    return this.#values.get(key) ?? null;
  }

  key(index: number): string | null {
    return [...this.#values.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.#values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.#values.set(key, value);
  }
}

describe("theme preference persistence", () => {
  it("falls back safely for corrupt, unknown, or old values", () => {
    expect(parseStoredThemePreference("not json")).toEqual(
      DEFAULT_THEME_PREFERENCE,
    );
    expect(
      parseStoredThemePreference(
        JSON.stringify({ version: 1, scheme: "sepia", skinId: "wb.default" }),
      ),
    ).toEqual(DEFAULT_THEME_PREFERENCE);
    expect(
      parseStoredThemePreference(
        JSON.stringify({ version: 1, scheme: "dark", skinId: "remote.css" }),
      ),
    ).toEqual(DEFAULT_THEME_PREFERENCE);
    expect(
      parseStoredThemePreference(
        JSON.stringify({ version: 0, scheme: "dark", skinId: "wb.default" }),
      ),
    ).toEqual(DEFAULT_THEME_PREFERENCE);
  });

  it("round-trips the closed versioned bootstrap mirror", () => {
    const storage = new MemoryStorage();
    writeThemePreference(
      { scheme: "dark", skinId: "wb.conformance-stress" },
      storage,
    );

    expect(JSON.parse(storage.getItem(THEME_PREFERENCE_STORAGE_KEY)!)).toEqual({
      version: 1,
      scheme: "dark",
      skinId: "wb.conformance-stress",
    });
    expect(readThemePreference(storage)).toEqual({
      scheme: "dark",
      skinId: "wb.conformance-stress",
    });
  });
});
