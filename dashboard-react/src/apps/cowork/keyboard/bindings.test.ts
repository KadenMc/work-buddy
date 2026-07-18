import { describe, expect, it } from "vitest";

import {
  DEFAULT_COWORK_NAV_BINDING,
  NAV_BINDING_PRESETS,
  isNavBindingPreset,
  resolveNavBinding,
} from "./bindings";

describe("Co-work nav binding presets", () => {
  it("defaults to the inverted house pair (j previous, k next)", () => {
    expect(DEFAULT_COWORK_NAV_BINDING).toEqual({ prev: "j", next: "k" });
    expect(NAV_BINDING_PRESETS.inverted).toEqual({ prev: "j", next: "k" });
  });

  it("offers a conventional vim pair (j next, k previous)", () => {
    expect(NAV_BINDING_PRESETS.vim).toEqual({ prev: "k", next: "j" });
  });
});

describe("resolveNavBinding", () => {
  it("maps the inverted preset id to the inverted pair", () => {
    expect(resolveNavBinding("inverted")).toEqual({ prev: "j", next: "k" });
  });

  it("maps the vim preset id to the vim pair (override)", () => {
    expect(resolveNavBinding("vim")).toEqual({ prev: "k", next: "j" });
  });

  it("degrades an unknown or missing value to the inverted default", () => {
    expect(resolveNavBinding(undefined)).toEqual(DEFAULT_COWORK_NAV_BINDING);
    expect(resolveNavBinding(null)).toEqual(DEFAULT_COWORK_NAV_BINDING);
    expect(resolveNavBinding("emacs")).toEqual(DEFAULT_COWORK_NAV_BINDING);
    expect(resolveNavBinding(42)).toEqual(DEFAULT_COWORK_NAV_BINDING);
  });
});

describe("isNavBindingPreset", () => {
  it("recognizes only the two known preset ids", () => {
    expect(isNavBindingPreset("inverted")).toBe(true);
    expect(isNavBindingPreset("vim")).toBe(true);
    expect(isNavBindingPreset("other")).toBe(false);
    expect(isNavBindingPreset(undefined)).toBe(false);
  });
});
