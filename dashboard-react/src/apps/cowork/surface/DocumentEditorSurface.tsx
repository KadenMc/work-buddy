import type { ComponentProps } from "react";

import {
  CoworkBridgeEditor,
  type CoworkBridgeEditorProps,
} from "../bridge";
import { useDocumentSessionContext } from "../session/DocumentSession";

type BridgeEditorProps = ComponentProps<typeof CoworkBridgeEditor>;

export interface DocumentEditorSurfaceProps {
  readonly activeLens?: BridgeEditorProps["activeLens"];
  readonly provenanceSelectionActionsActive?: BridgeEditorProps["provenanceSelectionActionsActive"];
  readonly onProvenanceSelectionAction?: BridgeEditorProps["onProvenanceSelectionAction"];
  readonly onInputProvenancePendingChange?: BridgeEditorProps["onInputProvenancePendingChange"];
  readonly onProvenanceIdentityStateChange?: BridgeEditorProps["onProvenanceIdentityStateChange"];
  readonly className?: string;
}

/**
 * Presentation-neutral host for the durable bridge editor. Full Co-work and
 * contextual panels consume the same DocumentSession rather than constructing
 * a scratch editor or another writable Y.Doc.
 */
export function DocumentEditorSurface({
  activeLens = "neutral",
  provenanceSelectionActionsActive = false,
  onProvenanceSelectionAction,
  onInputProvenancePendingChange,
  onProvenanceIdentityStateChange,
  className,
}: DocumentEditorSurfaceProps) {
  const session = useDocumentSessionContext();
  const bridge = session.bridge;
  return (
    <div
      className={["wb-document-editor-surface", className].filter(Boolean).join(" ")}
      data-document-session={session.key}
      data-document-writable={session.writable ? "true" : "false"}
    >
      <CoworkBridgeEditor
        {...bridge.editorProps}
        activeLens={activeLens}
        provenanceProvider={bridge.provenanceProvider}
        provenanceSelectionActionsActive={provenanceSelectionActionsActive}
        onProvenanceSelectionAction={onProvenanceSelectionAction}
        onInputProvenancePendingChange={onInputProvenancePendingChange}
        onProvenanceIdentityStateChange={onProvenanceIdentityStateChange}
      />
    </div>
  );
}

export type { CoworkBridgeEditorProps };
