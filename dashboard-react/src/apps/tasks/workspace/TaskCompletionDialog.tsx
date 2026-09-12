import { useRef, useState } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";
import type { IntentResult } from "../../../dashboard/contributions/contracts";
import { Button, InlineAlert } from "../../../ui";
import { createCorrelationId } from "../../../widget-library/shared";

export interface CompletionTask { readonly task_id: string; readonly title: string; readonly revision: number }

/** The reviewed task revision and mutation ID remain fixed through uncertain retries. */
export function TaskCompletionDialog({ task, onConfirm, onClose }: {
  readonly task: CompletionTask;
  onConfirm(clientMutationId: string): Promise<IntentResult>;
  onClose(): void;
}) {
  const mutationId = useRef(createCorrelationId("confirm-complete"));
  const pending = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const confirm = async () => {
    if (pending.current || conflict) return;
    pending.current = true; setBusy(true); setError(null);
    try {
      const result = await onConfirm(mutationId.current);
      if (result.status === "accepted") { onClose(); return; }
      setConflict(result.status === "conflict");
      setError(result.message ?? "Completion could not be confirmed. Retry the same request or cancel.");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Completion could not be confirmed. Retry the same request or cancel.");
    } finally { pending.current = false; setBusy(false); }
  };
  return <ModalOverlay isOpen isDismissable={!busy} isKeyboardDismissDisabled={busy} onOpenChange={(open) => { if (!open && !pending.current) onClose(); }} className="wb-task-confirm-overlay">
    <Modal className="wb-task-confirm-modal"><Dialog className="wb-task-confirm-dialog" role="alertdialog" aria-labelledby="wb-task-complete-title" aria-describedby="wb-task-complete-description" aria-busy={busy || undefined}>
      <Heading id="wb-task-complete-title" slot="title">Complete this task?</Heading>
      <p className="wb-task-confirm-name">{task.title}</p>
      <p id="wb-task-complete-description">This marks the task completed and removes it from an Open-only list. To undo it later, include Completed in Status and choose Reopen.</p>
      <p className="wb-task-muted">Unsaved field edits remain in your draft.</p>
      {error ? <InlineAlert tone={conflict ? "warning" : "danger"}>{error}{conflict ? " Cancel and review the latest task before trying again." : " Retrying checks the same completion request."}</InlineAlert> : null}
      <div className="wb-task-actions"><Button autoFocus disabled={busy} onClick={onClose}>Cancel</Button><Button variant="primary" disabled={busy || conflict} onClick={() => void confirm()}>{busy ? "Completing…" : error ? "Retry completion" : "Mark complete"}</Button></div>
    </Dialog></Modal>
  </ModalOverlay>;
}
