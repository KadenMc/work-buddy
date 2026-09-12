import { useCallback, useEffect, useRef, useState } from "react";
import type { IntentResult, JsonValue } from "../../../dashboard/contributions/contracts";
import { useWidgetDraft } from "../../../dashboard/drafts";
import type { TaskWorkspaceInput } from "../contracts";
import { DEFAULT_TASK_BROWSE_STATE, taskBrowseKey, taskBrowsePatch, taskBrowseState, type TaskBrowseState } from "./taskBrowseState";

interface QueryTransition {
  readonly id: number;
  readonly sourceKey: string;
  readonly entryKey: string;
  readonly targetKey: string;
  readonly patch: Record<string, JsonValue>;
  readonly accepted: boolean;
  readonly error?: string;
  readonly rollback?: { readonly value: TaskBrowseState; readonly revision: number };
}

/** Host-owned card state; detail and proposal field drafts keep their separate scopes. */
export function useTaskBrowseState({ input, activeBrowse, onRestore }: {
  readonly input: TaskWorkspaceInput;
  readonly activeBrowse: boolean;
  onRestore(patch: Record<string, JsonValue>): Promise<IntentResult>;
}) {
  const draft = useWidgetDraft("task-browse", DEFAULT_TASK_BROWSE_STATE, {
    isPristine: (value) => taskBrowseKey(value) === taskBrowseKey(DEFAULT_TASK_BROWSE_STATE),
    includeInClear: activeBrowse,
  });
  const onRestoreRef = useRef(onRestore);
  onRestoreRef.current = onRestore;
  const queryKey = taskBrowseKey(input.query);
  const savedKey = taskBrowseKey(draft.value);
  const entryKey = JSON.stringify([input.query.task, input.query.proposal, input.query.mode]);
  const currentRef = useRef({ queryKey, entryKey });
  currentRef.current = { queryKey, entryKey };
  const [transition, setTransition] = useState<QueryTransition | null>(null);
  const transitionRef = useRef(transition);
  const sequence = useRef(0);
  const commitTransition = useCallback((value: QueryTransition | null) => {
    transitionRef.current = value;
    setTransition(value);
  }, []);
  const beginTransition = useCallback((patch: Record<string, JsonValue>, rollback?: QueryTransition["rollback"]) => {
    const request: QueryTransition = {
      id: ++sequence.current, sourceKey: currentRef.current.queryKey,
      entryKey: currentRef.current.entryKey, targetKey: taskBrowseKey(patch), patch, accepted: false,
      ...(rollback === undefined ? {} : { rollback }),
    };
    commitTransition(request);
    const reject = (message: string) => {
      if (transitionRef.current?.id !== request.id) return;
      if (request.rollback && currentRef.current.queryKey === request.sourceKey && currentRef.current.entryKey === request.entryKey) {
        // Undo this clear only. A newer edit, reset, or navigation owns its own
        // state and must never be overwritten by a late failed response.
        draft.compareAndSet(request.rollback.revision, request.rollback.value);
      }
      commitTransition({ ...request, error: message });
    };
    // Clear notifies listeners before replacing its value. Defer navigation so
    // no synchronous provider update can persist the pre-clear draft again.
    void Promise.resolve().then(() => onRestoreRef.current(patch)).then((result) => {
      if (transitionRef.current?.id !== request.id) return;
      if (result.status === "accepted") commitTransition({ ...request, accepted: true });
      else reject(result.message ?? "The saved task view could not be restored. Retry or choose new filters.");
    }).catch((error: unknown) => {
      reject(error instanceof Error ? error.message : "The task view could not change. Retry or choose new filters.");
    });
  }, [commitTransition, draft.compareAndSet]);

  useEffect(() => draft.subscribeReset(() => {
    const beforeClear = draft.getSnapshot();
    beginTransition(taskBrowsePatch(DEFAULT_TASK_BROWSE_STATE), { value: beforeClear.value, revision: beforeClear.revision + 1 });
  }), [beginTransition, draft.getSnapshot, draft.subscribeReset]);

  useEffect(() => {
    if (!draft.ready || draft.error) return;
    const pending = transitionRef.current;
    if (pending) {
      if (pending.error) {
        // Leave the failed state visible and retryable while the same query is
        // displayed. A deliberate new URL/filter choice supersedes that request.
        if (queryKey === pending.sourceKey && entryKey === pending.entryKey) return;
      } else if (!pending.accepted || (queryKey !== pending.targetKey && queryKey === pending.sourceKey)) {
        return;
      }
      commitTransition(null);
    }
    const saved = draft.getSnapshot().value;
    const currentSavedKey = taskBrowseKey(saved);
    if (input.browseQueryExplicit === false && currentSavedKey !== queryKey) {
      beginTransition({ ...taskBrowsePatch(saved), ...(input.query.mode === "namespaces" ? { mode: "namespaces" } : {}) });
      return;
    }
    if (currentSavedKey !== queryKey) draft.setValue(taskBrowseState(input.query));
  }, [beginTransition, commitTransition, draft.ready, draft.error, draft.getSnapshot, draft.setValue, input.browseQueryExplicit, queryKey, savedKey, entryKey, transition]);

  const implicitRestore = input.browseQueryExplicit === false && savedKey !== queryKey;
  const failed = Boolean(transition?.error);
  return {
    ready: (input.browseQueryExplicit !== false || draft.ready) && (Boolean(draft.error) || failed || (transition === null && !implicitRestore)),
    error: draft.error ?? transition?.error,
    canRetry: !draft.error && failed,
    retry: () => { if (transitionRef.current?.error) beginTransition(transitionRef.current.patch); },
  };
}
