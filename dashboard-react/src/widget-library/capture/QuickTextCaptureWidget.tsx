import type { WidgetRendererProps } from "../../dashboard/contributions/contracts";
import { createWidgetIntent } from "../shared";
import { CaptureComposer } from "./CaptureComposer";
import type {
  CaptureDraftRequest,
  CaptureSubmitIntent,
  QuickTextCaptureInput,
} from "./contracts";

export default function QuickTextCaptureWidget({
  input,
  emit,
  presentation,
}: WidgetRendererProps<QuickTextCaptureInput, CaptureSubmitIntent>) {
  const submit = (request: CaptureDraftRequest) => {
    const intent = createWidgetIntent(
      presentation,
      "wb.capture.submit",
      {
        day_id: request.dayId,
        target_id: request.targetId,
        mode: request.mode,
        exact_text: request.exactText,
      },
      {
        intentId: request.clientMutationId,
        clientMutationId: request.clientMutationId,
      },
    ) as CaptureSubmitIntent;
    emit(intent);
  };

  return (
    <CaptureComposer
      input={input}
      density={presentation.sizeMode}
      onSubmit={submit}
    />
  );
}
