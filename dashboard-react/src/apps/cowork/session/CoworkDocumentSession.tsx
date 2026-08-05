import type { CoworkDocumentSummary } from "../contracts";
import type { CoworkSyncStatus } from "../persistence/CoworkYdocPersistence";
import type {
  CoworkMaterializationController,
  CoworkMaterializationState,
  CoworkMaterializeReceipt,
} from "../materialization/contracts";
import { CoworkLiveWorkspace, healthFromDocument } from "../surface/CoworkWorkspaceSurface";
import type {
  CoworkInvitePerspectiveHandler,
  CoworkRunVerifyHandler,
} from "../targets";
import type {
  TruthPassageConnection,
  TruthPassageNavigationTarget,
} from "../truth";

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
  readonly onRunVerify?: CoworkRunVerifyHandler;
  readonly onInvitePerspective?: CoworkInvitePerspectiveHandler;
  readonly onOpenTruthPassage?: (connection: TruthPassageConnection) => void;
  readonly pendingTruthPassageNavigation?: TruthPassageNavigationTarget | null;
  readonly onTruthPassageNavigationConsumed?: (requestId: string) => void;
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
  onRunVerify,
  onInvitePerspective,
  onOpenTruthPassage,
  pendingTruthPassageNavigation,
  onTruthPassageNavigationConsumed,
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
      onRunVerify={onRunVerify}
      onInvitePerspective={onInvitePerspective}
      onOpenTruthPassage={onOpenTruthPassage}
      pendingTruthPassageNavigation={pendingTruthPassageNavigation}
      onTruthPassageNavigationConsumed={onTruthPassageNavigationConsumed}
    />
  );
}
