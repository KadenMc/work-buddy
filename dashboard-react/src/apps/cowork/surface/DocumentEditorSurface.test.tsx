import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { CoworkBridge } from "../bridge";
import {
  DocumentSessionProvider,
  type DocumentSession,
} from "../session/DocumentSession";
import { DocumentEditorSurface } from "./DocumentEditorSurface";

const editorProps = vi.hoisted(() => vi.fn());
vi.mock("../bridge", () => ({
  CoworkBridgeEditor: (props: unknown) => {
    editorProps(props);
    return <div data-testid="bridge-editor" />;
  },
  useCoworkBridge: vi.fn(),
}));

describe("DocumentEditorSurface", () => {
  it("renders the registered bridge rather than a scratch editor", () => {
    const bridge = {
      editorProps: { documentId: "doc-1", storeId: "store-1" },
      provenanceProvider: { kind: "provider" },
    } as unknown as CoworkBridge;
    const session: DocumentSession = {
      key: JSON.stringify(["store-1", "doc-1"]),
      reference: { kind: "workspace", storeId: "store-1", documentId: "doc-1" },
      bridge,
      writable: true,
      syncStatus: "clean",
    };
    render(
      <DocumentSessionProvider session={session}>
        <DocumentEditorSurface activeLens="provenance" />
      </DocumentSessionProvider>,
    );

    expect(screen.getByTestId("bridge-editor")).toBeInTheDocument();
    expect(editorProps).toHaveBeenCalledWith(expect.objectContaining({
      documentId: "doc-1",
      storeId: "store-1",
      activeLens: "provenance",
      provenanceProvider: bridge.provenanceProvider,
    }));
  });
});
