import { describe, expect, it } from "vitest";
import { ySyncPluginKey } from "@tiptap/y-tiptap";
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

  it("classifies only the ySync binding origin as a live human edit", () => {
    // Human keystrokes sync to the Y.Doc under ySyncPluginKey. Every other origin,
    // including a bare null transaction, an undefined origin, and the apply-origin
    // tag, is excluded from R4, so a future bare doc.transact never leaks.
    expect(isLocalHumanOrigin(ySyncPluginKey)).toBe(true);
    expect(isLocalHumanOrigin(null)).toBe(false);
    expect(isLocalHumanOrigin(undefined)).toBe(false);
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
