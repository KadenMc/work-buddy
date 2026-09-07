import { StrictMode, useEffect } from "react";
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { InMemoryCoworkYdocTransport } from "../persistence/InMemoryCoworkYdocTransport";
import { useCoworkBridge } from "./useCoworkBridge";

describe("useCoworkBridge lifetime", () => {
  it("preserves the live document and replayed observers until actual unmount", async () => {
    const onUpdate = vi.fn();
    const hook = renderHook(
      () => {
        const bridge = useCoworkBridge({
          documentId: "doc-lifetime",
          storeId: "store-lifetime",
          docClient: {
            fetchDoc: async () => {
              throw new Error("No provenance payload in this lifecycle fixture");
            },
          },
          ydocTransport: new InMemoryCoworkYdocTransport(),
        });
        const document = bridge.editorProps.document;
        useEffect(() => {
          document.on("update", onUpdate);
          return () => document.off("update", onUpdate);
        }, [document]);
        return bridge;
      },
      { wrapper: StrictMode },
    );
    const document = hook.result.current.editorProps.document;
    await act(async () => {
      await Promise.resolve();
    });

    expect(document.isDestroyed).toBe(false);
    document.getText("t").insert(0, "still observed");
    expect(onUpdate).toHaveBeenCalledOnce();

    hook.unmount();
    await Promise.resolve();
    expect(document.isDestroyed).toBe(true);
  });
});
