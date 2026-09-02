import { useCallback, useLayoutEffect, useRef, type ReactNode } from "react";

import {
  WorkspaceSidePanel,
  type WorkspaceSidePanelMode,
} from "../../../dashboard/layout/WorkspaceSidePanel";
import { Button } from "../../../ui";
import {
  coworkSessionDurability,
  registeredSessionDurabilityKey,
} from "../session/CoworkSessionDurability";
import {
  DocumentSessionProvider,
  type BoundDocumentRef,
  type DocumentSession,
} from "../session/DocumentSession";
import {
  DocumentEditorSurface,
  type DocumentEditorSurfaceProps,
} from "./DocumentEditorSurface";
import "./DocumentWorkspacePanel.css";

const STATUS_LABEL: Readonly<Record<DocumentSession["syncStatus"], string>> = {
  hydrating: "Opening…",
  clean: "Saved",
  saving: "Saving…",
  saved_on_device: "Saved on this device",
  retrying: "Retrying…",
  offline: "Offline · saved on this device",
  conflict: "Sync conflict",
  error: "Sync needs attention",
  read_only: "Read only",
};

export interface DocumentWorkspacePanelProps {
  readonly reference: BoundDocumentRef;
  readonly session: DocumentSession;
  readonly primary: ReactNode;
  readonly title: string;
  readonly mode?: WorkspaceSidePanelMode;
  readonly layoutId?: string;
  readonly editor?: DocumentEditorSurfaceProps;
  /** Server-resolved Open-full entitlement; false omits the action entirely. */
  readonly canOpenFull: boolean;
  readonly onClose: () => void | Promise<void>;
  readonly onOpenFull: (reference: BoundDocumentRef) => void | Promise<void>;
}

async function crossDurabilityBarrier(
  reference: BoundDocumentRef,
  operation: () => void | Promise<void>,
): Promise<void> {
  const lease = await coworkSessionDurability.prepareToLeave(
    registeredSessionDurabilityKey(reference.storeId, reference.documentId),
  );
  try {
    await operation();
    lease?.commit();
  } catch (error) {
    lease?.cancel();
    throw error;
  }
}

/** Host detail plus the same durable document session in a contextual panel. */
export function DocumentWorkspacePanel({
  reference,
  session,
  primary,
  title,
  mode = "split",
  layoutId = `wb.document-panel:${reference.binding.domain.namespace}:${reference.binding.domain.kind}`,
  editor,
  canOpenFull,
  onClose,
  onOpenFull,
}: DocumentWorkspacePanelProps) {
  if (
    session.reference.storeId !== reference.storeId ||
    session.reference.documentId !== reference.documentId
  ) {
    throw new Error("The contextual panel session does not match its bound document reference.");
  }
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    openerRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    headingRef.current?.focus({ preventScroll: true });
  }, [session.key]);
  const close = useCallback(async (): Promise<void> => {
    await crossDurabilityBarrier(reference, onClose);
    window.requestAnimationFrame(() => openerRef.current?.focus({ preventScroll: true }));
  }, [onClose, reference]);
  const openFull = useCallback(
    () => crossDurabilityBarrier(reference, () => onOpenFull(reference)),
    [onOpenFull, reference],
  );

  return (
    <DocumentSessionProvider session={session}>
      <div className="wb-document-workspace-panel" data-document-session={session.key}>
        <WorkspaceSidePanel
          layoutId={layoutId}
          primaryId="domain-detail"
          sideId="bound-document"
          mode={mode}
          primary={primary}
          resizeLabel="Resize the document panel"
          sideMinSize="20rem"
          sideDefaultSize="42%"
          primaryClassName="wb-document-workspace-panel__primary"
          sideClassName="wb-document-workspace-panel__document"
          side={
            <aside aria-labelledby="wb-bound-document-heading">
              <header className="wb-document-workspace-panel__header">
                <div>
                  <h2 id="wb-bound-document-heading" ref={headingRef} tabIndex={-1}>
                    {title}
                  </h2>
                  <p role="status" aria-live="polite">
                    {STATUS_LABEL[session.syncStatus]}
                  </p>
                </div>
                <div className="wb-document-workspace-panel__actions">
                  {canOpenFull ? (
                    <Button size="small" onClick={() => void openFull()}>
                      Open full
                    </Button>
                  ) : null}
                  <Button size="small" variant="ghost" onClick={() => void close()}>
                    Close
                  </Button>
                </div>
              </header>
              <div className="wb-document-workspace-panel__editor">
                <DocumentEditorSurface {...editor} />
              </div>
            </aside>
          }
        />
      </div>
    </DocumentSessionProvider>
  );
}
