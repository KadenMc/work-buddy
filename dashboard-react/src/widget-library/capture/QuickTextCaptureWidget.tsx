import type { WidgetRendererProps } from "../../dashboard/contributions/contracts";
import { createWidgetIntent } from "../shared";
import { CaptureComposer } from "./CaptureComposer";
import type {
  CaptureDraftRequest,
  CaptureSubmitIntent,
  CaptureIntent,
  CaptureSubmissionRecord,
  QuickTextCaptureInput,
} from "./contracts";
import { captureSmartDisclosureSha256 } from "./smartDisclosure";

export default function QuickTextCaptureWidget({
  input,
  emit,
  presentation,
}: WidgetRendererProps<QuickTextCaptureInput, CaptureIntent>) {
  const submit = (request: CaptureDraftRequest) => {
    const intent = createWidgetIntent(
      presentation,
      "wb.capture.submit",
      {
        day_id: request.dayId,
        target_id: request.targetId,
        mode: request.mode,
        exact_text: request.exactText,
        ...(request.statedAt ? { stated_at: request.statedAt } : {}),
        ...(request.followUpActionId ? { follow_up_action: request.followUpActionId } : {}),
        ...(request.smartDisclosureSha256 ? { smart_disclosure_sha256: request.smartDisclosureSha256 } : {}),
      },
      {
        intentId: request.clientMutationId,
        clientMutationId: request.clientMutationId,
      },
    ) as CaptureSubmitIntent;
    return emit(intent);
  };

  const retry = async (capture: CaptureSubmissionRecord) => {
    // Hash the displayed snapshot before yielding; provider refreshes must not
    // silently change the boundary of this already-clicked retry gesture.
    const captureId = capture.captureId!;
    const expectedRevision = capture.revision!;
    const reviewedDisclosure = captureSmartDisclosureSha256(
      capture.mode === "smart" ? input.smartAvailability?.disclosure : undefined,
    );
    const smartDisclosureSha256 = await reviewedDisclosure;
    return emit(createWidgetIntent(presentation, "wb.capture.retry-requested", {
      capture_id: captureId, expected_revision: expectedRevision,
      ...(smartDisclosureSha256 ? { smart_disclosure_sha256: smartDisclosureSha256 } : {}),
    }) as CaptureIntent);
  };

  return (
    <CaptureComposer
      input={input}
      density={presentation.sizeMode}
      onSubmit={submit}
      onRefreshAvailability={() => emit(createWidgetIntent(presentation, "wb.capture.availability-refresh", {}) as CaptureIntent)}
      onRetry={retry}
    />
  );
}
