import { describe, expect, it } from "vitest";
import * as Y from "yjs";

import {
  COWORK_APPLY_ORIGIN,
  applyForeignUpdate,
  applyWithOrigin,
  isLocalHumanOrigin,
} from "./applyOrigin";

describe("apply-origin discipline", () => {
  it("tags a local ledger-derived mutation with the apply-origin origin", () => {
    const doc = new Y.Doc();
    const origins: unknown[] = [];
    doc.on("update", (_update: Uint8Array, origin: unknown) => origins.push(origin));

    applyWithOrigin(doc, () => {
      doc.getMap("meta").set("k", "v");
    });

    expect(doc.getMap("meta").get("k")).toBe("v");
    expect(origins).toEqual([COWORK_APPLY_ORIGIN]);
  });

  it("applies a foreign update under the apply-origin origin", () => {
    const source = new Y.Doc();
    source.getArray("body").insert(0, ["one", "two"]);
    const update = Y.encodeStateAsUpdate(source);

    const target = new Y.Doc();
    const origins: unknown[] = [];
    target.on("update", (_update: Uint8Array, origin: unknown) => origins.push(origin));

    applyForeignUpdate(target, update);

    expect(target.getArray("body").toArray()).toEqual(["one", "two"]);
    expect(origins).toEqual([COWORK_APPLY_ORIGIN]);
  });

  it("distinguishes a live human origin from the apply-origin tag", () => {
    // A live local edit carries a null origin, and only apply-origin mutations are excluded.
    expect(isLocalHumanOrigin(null)).toBe(true);
    expect(isLocalHumanOrigin(undefined)).toBe(true);
    expect(isLocalHumanOrigin(COWORK_APPLY_ORIGIN)).toBe(false);
  });

  it("re-applying the same foreign update is idempotent", () => {
    const source = new Y.Doc();
    source.getArray("body").insert(0, ["x"]);
    const update = Y.encodeStateAsUpdate(source);

    const target = new Y.Doc();
    applyForeignUpdate(target, update);
    applyForeignUpdate(target, update);

    expect(target.getArray("body").toArray()).toEqual(["x"]);
  });
});
