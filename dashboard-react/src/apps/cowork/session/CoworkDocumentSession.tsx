import type { CoworkDocumentSummary } from "../contracts";
import type { CoworkSyncStatus } from "../persistence/CoworkYdocPersistence";
import type {
  CoworkMaterializationController,
  CoworkMaterializationState,
  CoworkMaterializeReceipt,
} from "../materialization/contracts";
import { CoworkLiveWorkspace, healthFromDocument } from "../surface/CoworkWorkspaceSurface";

export interface CoworkDocumentSessionProps {
  readonly storeId: string;
  readonly document: CoworkDocumentSummary;
  readonly feedbackCapture?: boolean;
  readonly onSyncStatus?: (status: CoworkSyncStatus) => void;
  readonly onMaterializationState?: (state: CoworkMaterializationState) => void;
  readonly onMaterializationController?: (
    controller: CoworkMaterializationController | null,
  ) => void;
  readonly onMaterialized?: (receipt: CoworkMaterializeReceipt) => void;
}

/** Key this boundary by store/document so no live editor resource crosses a switch. */
export function CoworkDocumentSession({
  storeId,
  document,
  feedbackCapture = true,
  onSyncStatus,
  onMaterializationState,
  onMaterializationController,
  onMaterialized,
}: CoworkDocumentSessionProps) {
  return (
    <CoworkLiveWorkspace
      documentId={document.documentId}
      storeId={storeId}
      document={document}
      fallbackHealth={healthFromDocument(document)}
      showHealth={false}
      readOnly={document.permissions?.edit === false}
      feedbackCapture={feedbackCapture}
      onSyncStatus={onSyncStatus}
      onMaterializationState={onMaterializationState}
      onMaterializationController={onMaterializationController}
      onMaterialized={onMaterialized}
    />
  );
}
