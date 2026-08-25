import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefCallback,
} from "react";

import type {
  ProvenanceAttestation,
  ProvenanceEditorIntegration,
  ProvenanceLoad,
  ProvenanceProvider,
  ProvenanceTarget,
  ProvenanceMutationBarrier,
  ProvenanceSelectionAction,
} from "./contracts";
import {
  provenanceDisplayedAxesFingerprint,
  provenancePersonDetail,
  provenanceSourceDetails,
} from "./semantics";
import { asCoworkApiError, coworkErrorMessage } from "../../providers/errors";

type ProvenanceFilter = "all" | "needs_review" | "ai_authored" | "issues";

export interface ProvenancePanelProps {
  readonly provider: ProvenanceProvider;
  readonly active: boolean;
  readonly scrollContainerRef?: RefCallback<HTMLElement>;
  readonly editor?: ProvenanceEditorIntegration;
  readonly readOnly?: boolean;
  readonly mutationBarrier?: ProvenanceMutationBarrier;
  /** Opens stable detail for a provenance-aware editor selection action. */
  readonly selectionAction?: ProvenanceSelectionAction | null;
  /** Browser-local direct entry is awaiting an authoritative ledger receipt. */
  readonly inputProvenancePending?: boolean;
  /** Visible reason human review mutations are temporarily unavailable. */
  readonly mutationBlockedReason?: string;
}

const sourceLabel = (attestation: ProvenanceAttestation): string => {
  const kind = attestation.source.kind;
  if (kind === "legacy") return "Untracked / legacy";
  return typeof kind === "string" ? kind.replace(/_/gu, " ") : "Unknown source";
};

const passageExcerpt = (value: string, max = 180): string =>
  value.length <= max ? value : `${value.slice(0, max - 1)}…`;

const mutationErrorMessage = (cause: unknown): string => {
  const error = asCoworkApiError(cause);
  if (
    error.code.includes("stale") ||
    error.code.includes("conflict") ||
    error.status === 409
  ) {
    return "The document or provenance record changed. The latest view was requested; inspect the passage before trying again.";
  }
  if (error.code.includes("actor") || error.code.includes("identity")) {
    return "Your review identity changed. Refresh Co-work, confirm your identity, and try again.";
  }
  return coworkErrorMessage(
    error,
    error.retryable
      ? "This review was not confirmed. Retry will safely reuse the same request."
      : "This passage could not be marked reviewed.",
  );
};

const targetId = (target: ProvenanceTarget): string => target.projectionId;

const effective = (target: ProvenanceTarget): ProvenanceAttestation | null =>
  target.resolution === "resolved" ? target.effectiveAttestation : null;

type AnchorState =
  "unique" | "missing" | "ambiguous" | "document" | "unavailable";

interface ResolvedPanelTarget {
  readonly target: ProvenanceTarget;
  readonly payloadOrder: number;
  readonly anchorResolution: {
    readonly state: AnchorState;
    readonly documentOrder: number | null;
    readonly documentEnd: number | null;
  };
  readonly peerConflictIds: readonly string[];
}

const targetAxes = (target: ProvenanceTarget): readonly string[] =>
  target.effectiveAttestations.map(provenanceDisplayedAxesFingerprint);

const incompatibleOverlap = (
  left: ResolvedPanelTarget,
  right: ResolvedPanelTarget,
): boolean => {
  if (
    left.target.span === null ||
    right.target.span === null ||
    left.target.target.currentness === "stale" ||
    left.target.target.currentness === "unavailable" ||
    right.target.target.currentness === "stale" ||
    right.target.target.currentness === "unavailable" ||
    left.anchorResolution.state !== "unique" ||
    right.anchorResolution.state !== "unique" ||
    left.anchorResolution.documentOrder === null ||
    right.anchorResolution.documentOrder === null ||
    left.anchorResolution.documentEnd === null ||
    right.anchorResolution.documentEnd === null ||
    left.anchorResolution.documentOrder >= right.anchorResolution.documentEnd ||
    right.anchorResolution.documentOrder >= left.anchorResolution.documentEnd
  ) {
    return false;
  }
  return (
    new Set([...targetAxes(left.target), ...targetAxes(right.target)]).size > 1
  );
};

const withPeerConflicts = (
  items: readonly Omit<ResolvedPanelTarget, "peerConflictIds">[],
): readonly ResolvedPanelTarget[] =>
  items.map((item, index) => ({
    ...item,
    peerConflictIds: items.flatMap((candidate, candidateIndex) =>
      candidateIndex !== index &&
      incompatibleOverlap(
        { ...item, peerConflictIds: [] },
        { ...candidate, peerConflictIds: [] },
      )
        ? [candidate.target.projectionId]
        : [],
    ),
  }));

const resolvePanelTargets = (
  view: Extract<ProvenanceLoad, { readonly state: "ready" }>["data"],
  editor: ProvenanceEditorIntegration | undefined,
): readonly ResolvedPanelTarget[] =>
  [
    ...withPeerConflicts(
      [
        ...(view.documentDefault === null ? [] : [view.documentDefault]),
        ...view.spans,
      ].map((target, payloadOrder) => {
        const anchorResolution =
          target.span === null && target.target.kind === "document_version"
            ? {
                state: "document" as const,
                documentOrder: -1,
                documentEnd: null,
              }
            : target.span === null
              ? {
                  state: "unavailable" as const,
                  documentOrder: null,
                  documentEnd: null,
                }
              : (editor?.resolveTarget(target.span) ?? {
                  state: "missing" as const,
                  documentOrder: null,
                  documentEnd: null,
                });
        return { target, payloadOrder, anchorResolution };
      }),
    ),
  ].sort((left, right) => {
    const leftOrder = left.anchorResolution.documentOrder;
    const rightOrder = right.anchorResolution.documentOrder;
    if (leftOrder !== null && rightOrder !== null)
      return leftOrder - rightOrder;
    if (leftOrder !== null) return -1;
    if (rightOrder !== null) return 1;
    return left.payloadOrder - right.payloadOrder;
  });

const statusLabel = (
  target: ProvenanceTarget,
  anchorState: AnchorState,
): string => {
  if (anchorState === "unavailable") return "Passage unavailable";
  if (target.resolution === "conflicted") return "Conflicting records";
  if (anchorState === "missing") return "Passage not found";
  if (anchorState === "ambiguous") return "Matches multiple passages";
  if (target.target.currentness === "stale") return "Stale target";
  if (target.target.currentness === "unavailable") return "Target unavailable";
  if (target.target.currentness === "requires_reanchor") {
    return anchorState === "unique"
      ? "Reanchored for inspection"
      : "Needs location check";
  }
  const record = effective(target);
  if (record === null) return "Unrecorded";
  const author =
    record.authorship.kind === "ai" ? "AI" : record.authorship.kind;
  const review = record.humanReview.status.replace(/_/gu, " ");
  return `${author} · ${review}`;
};

const canReview = (
  target: ProvenanceTarget,
  anchorState: AnchorState,
  hasPeerConflict = false,
  locallyDirty = false,
): boolean => {
  const record = effective(target);
  return (
    !locallyDirty &&
    !hasPeerConflict &&
    target.reviewEligibility === "eligible" &&
    target.target.currentness === "current" &&
    (anchorState === "unique" || anchorState === "document") &&
    target.resolution === "resolved" &&
    record !== null &&
    (record.authorship.kind === "ai" || record.authorship.kind === "mixed") &&
    (record.humanReview.status === "not_reviewed" ||
      record.humanReview.status === "unknown")
  );
};

const reviewBlockedReason = (target: ProvenanceTarget): string => {
  if (target.resolution === "conflicted") {
    return "Resolve the conflicting attestations before recording review.";
  }
  if (target.target.currentness === "stale") {
    return "This record targets an older document version.";
  }
  if (target.target.currentness === "requires_reanchor") {
    return "This passage is re-anchored for inspection, but recording review requires an unchanged document head.";
  }
  const record = effective(target);
  if (record === null) return "No single effective attestation is available.";
  if (record.humanReview.status === "reviewed") return "Already reviewed.";
  if (record.authorship.kind !== "ai" && record.authorship.kind !== "mixed") {
    return "Review attribution is available only for AI or mixed authorship.";
  }
  return "This provenance record is inspect-only.";
};

const matches = (
  target: ProvenanceTarget,
  filter: ProvenanceFilter,
  anchorState: AnchorState,
  hasPeerConflict = false,
): boolean => {
  const record = effective(target);
  if (filter === "all") return true;
  if (filter === "issues") {
    return (
      hasPeerConflict ||
      target.resolution === "conflicted" ||
      target.target.currentness === "stale" ||
      target.target.currentness === "requires_reanchor" ||
      target.target.currentness === "unavailable" ||
      anchorState === "missing" ||
      anchorState === "ambiguous"
    );
  }
  if (filter === "needs_review") {
    return (
      (record?.authorship.kind === "ai" ||
        record?.authorship.kind === "mixed") &&
      (record.humanReview.status === "not_reviewed" ||
        record.humanReview.status === "unknown")
    );
  }
  return (
    record?.authorship.kind === "ai" || record?.authorship.kind === "mixed"
  );
};

const isIssue = (item: ResolvedPanelTarget, locallyDirty: boolean): boolean =>
  locallyDirty ||
  matches(
    item.target,
    "issues",
    item.anchorResolution.state,
    item.peerConflictIds.length > 0,
  );

const shortIdentity = (value: string | null): string | null =>
  value === null || value.length <= 12
    ? value
    : `${value.slice(0, 6)}…${value.slice(-4)}`;

function RecordDetails({ record }: { readonly record: ProvenanceAttestation }) {
  const attester = record.assertedBy.ref ?? record.assertedBy.kind;
  const sourceDetails = provenanceSourceDetails(record);
  return (
    <dl className="wb-cowork-provenance-panel__facts">
      <div>
        <dt>Authorship</dt>
        <dd>{record.authorship.kind}</dd>
      </div>
      <div>
        <dt>Contributors</dt>
        <dd>
          {record.authorship.contributors
            .map(provenancePersonDetail)
            .join(", ") || "None recorded"}
        </dd>
      </div>
      <div>
        <dt>Human review</dt>
        <dd>{record.humanReview.status.replace(/_/gu, " ")}</dd>
      </div>
      <div>
        <dt>Reviewers</dt>
        <dd>
          {record.humanReview.reviewers
            .map(provenancePersonDetail)
            .join(", ") || "None recorded"}
        </dd>
      </div>
      <div>
        <dt>Source</dt>
        <dd>{sourceLabel(record)}</dd>
      </div>
      {sourceDetails.length === 0 ? null : (
        <div>
          <dt>Source details</dt>
          <dd>
            {sourceDetails
              .map((detail) => `${detail.label}: ${detail.value}`)
              .join(" · ")}
          </dd>
        </div>
      )}
      <div>
        <dt>Attested by</dt>
        <dd>{attester}</dd>
      </div>
      <div>
        <dt>Basis</dt>
        <dd>{record.basis.kind.replace(/_/gu, " ")}</dd>
      </div>
      <div>
        <dt>Target</dt>
        <dd>
          {record.scope.kind === "document_span"
            ? "Passage"
            : "Document version"}
          {record.scope.documentSpanId === null
            ? ""
            : ` · ${shortIdentity(record.scope.documentSpanId)}`}
          {record.scope.documentVersionId === null
            ? ""
            : ` · ${shortIdentity(record.scope.documentVersionId)}`}
        </dd>
      </div>
      <div>
        <dt>Recorded</dt>
        <dd>
          <time dateTime={record.at}>
            {new Date(record.at).toLocaleString()}
          </time>
        </dd>
      </div>
    </dl>
  );
}

function ProvenanceDetails({ target }: { readonly target: ProvenanceTarget }) {
  const record = effective(target);
  if (record === null) {
    return (
      <div className="wb-cowork-provenance-panel__conflict">
        <p className="wb-cowork-provenance-panel__reason">
          {target.issue?.message ??
            `${String(target.effectiveAttestations.length)} effective records disagree; none is treated as authoritative.`}
        </p>
        {target.effectiveAttestations.map((leaf) => (
          <article key={leaf.attestationId}>
            <h4>Effective record</h4>
            <RecordDetails record={leaf} />
          </article>
        ))}
      </div>
    );
  }
  return (
    <>
      <RecordDetails record={record} />
      <details className="wb-cowork-provenance-panel__history">
        <summary>
          {target.history.length} immutable{" "}
          {target.history.length === 1 ? "record" : "records"}
        </summary>
        <ol>
          {target.history.map((entry) => (
            <li key={entry.attestationId}>
              <RecordDetails record={entry} />
              <p>
                {entry.supersedesId === null
                  ? "Original record"
                  : "Supersedes an earlier record"}
              </p>
            </li>
          ))}
        </ol>
      </details>
    </>
  );
}

function ProvenanceContents({
  load,
  provider,
  editor,
  mutationBarrier,
  readOnly,
  selectionAction,
  inputProvenancePending = false,
  mutationBlockedReason,
}: {
  readonly load: ProvenanceLoad;
  readonly provider: ProvenanceProvider;
  readonly editor?: ProvenanceEditorIntegration;
  readonly readOnly?: boolean;
  readonly mutationBarrier?: ProvenanceMutationBarrier;
  readonly selectionAction?: ProvenanceSelectionAction | null;
  readonly inputProvenancePending?: boolean;
  readonly mutationBlockedReason?: string;
}) {
  const view = load.state === "ready" ? load.data : undefined;
  const [filter, setFilter] = useState<ProvenanceFilter>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const mutationLock = useRef(false);
  const routedRowRef = useRef<HTMLButtonElement | null>(null);
  const routedSelectionReviewRef = useRef<HTMLButtonElement | null>(null);
  const routedSelectionReviewCardRef = useRef<HTMLElement | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [completedSelectionFingerprint, setCompletedSelectionFingerprint] =
    useState<string | null>(null);
  useEffect(() => {
    if (
      selectionAction === null ||
      selectionAction === undefined ||
      selectionAction.intent === "record" ||
      selectionAction.targetIds.length === 0
    ) {
      return;
    }
    setFilter("all");
    setSelectedId(selectionAction.targetIds[0]!);
  }, [selectionAction]);
  useLayoutEffect(() => {
    if (selectionAction?.intent === "review") {
      const action = routedSelectionReviewRef.current;
      const focusTarget =
        action !== null && !action.disabled
          ? action
          : routedSelectionReviewCardRef.current;
      if (focusTarget === null) return;
      focusTarget.focus({ preventScroll: true });
      focusTarget.scrollIntoView?.({ block: "nearest" });
      return;
    }
    const target = selectionAction?.targetIds[0];
    if (
      target === undefined ||
      selectionAction?.intent === "record" ||
      selectedId !== target
    ) {
      return;
    }
    const row = routedRowRef.current;
    if (row === null) return;
    row.focus({ preventScroll: true });
    row.scrollIntoView?.({ block: "nearest" });
  }, [
    load,
    mutationBarrier,
    mutationBlockedReason,
    readOnly,
    selectedId,
    selectionAction,
  ]);
  if (view === undefined) {
    return (
      <div className="wb-cowork-provenance-panel__unavailable" role="status">
        <h2>Provenance view unavailable</h2>
        <p>
          {load.state === "unavailable"
            ? load.reason
            : "This server did not provide the provenance view needed to explain the document safely."}
        </p>
      </div>
    );
  }
  const targets = resolvePanelTargets(view, editor);
  const locallyDirty = editor?.isLocallyDirty() ?? false;
  const visible = targets.filter((item) =>
    filter === "issues"
      ? isIssue(item, locallyDirty)
      : matches(
          item.target,
          filter,
          item.anchorResolution.state,
          item.peerConflictIds.length > 0,
        ),
  );
  const needsReviewCount = targets.filter((item) =>
    matches(
      item.target,
      "needs_review",
      item.anchorResolution.state,
      item.peerConflictIds.length > 0,
    ),
  ).length;
  const reviewedCount = targets.filter(
    ({ target }) => effective(target)?.humanReview.status === "reviewed",
  ).length;
  const issueCount = targets.filter((item) =>
    isIssue(item, locallyDirty),
  ).length;
  const unrecorded =
    (editor?.hasText() ?? true) &&
    (locallyDirty ||
      view.documentDefault === null ||
      view.documentDefault.target.currentness !== "current" ||
      view.documentDefault.effectiveAttestations.length === 0) &&
    (editor?.hasUncoveredText(
      view.spans.flatMap((target) =>
        target.span !== null &&
        target.effectiveAttestations.length > 0 &&
        target.target.currentness !== "stale" &&
        target.target.currentness !== "unavailable"
          ? [target.span]
          : [],
      ),
    ) ??
      true);
  const selected =
    targets.find((item) => targetId(item.target) === selectedId)?.target ??
    null;
  const hasText = editor?.hasText() ?? true;
  const noRecordedProvenance =
    !inputProvenancePending &&
    !locallyDirty &&
    hasText &&
    targets.length === 0 &&
    view.history.length === 0;
  const selectionReviewFingerprint =
    selectionAction?.intent === "review"
      ? JSON.stringify({
          requestId: selectionAction.requestId,
          anchor: selectionAction.anchor,
          from: selectionAction.from,
          to: selectionAction.to,
          targetIds: selectionAction.targetIds,
          coversWholeDocument: selectionAction.coversWholeDocument ?? false,
          reviewer: selectionAction.reviewer,
        })
      : null;
  const selectionReviewItems =
    selectionAction?.intent === "review" &&
    selectionReviewFingerprint !== completedSelectionFingerprint
      ? selectionAction.targetIds.map((id) =>
          targets.find(
            (candidate) => candidate.target.projectionId === id,
          ),
        )
      : [];
  const selectionReviewTargets = selectionReviewItems.some(
    (item) => item === undefined,
  )
    ? []
    : selectionReviewItems.flatMap((item) =>
        item === undefined ? [] : [item.target],
      );
  const selectionReviewTargetsAreCurrent =
    selectionReviewTargets.length > 0 &&
    selectionReviewItems.every((item) => {
      if (item === undefined) return false;
      const record = effective(item.target);
      const reviewer = selectionAction?.reviewer;
      const targetIsContained =
        item.target.span === null
          ? selectionAction?.coversWholeDocument === true
          : selectionAction?.intent === "review" &&
            item.anchorResolution.documentOrder !== null &&
            item.anchorResolution.documentEnd !== null &&
            selectionAction.from <= item.anchorResolution.documentOrder &&
            selectionAction.to >= item.anchorResolution.documentEnd;
      return (
        item.peerConflictIds.length === 0 &&
        item.target.resolution === "resolved" &&
        item.target.target.currentness === "current" &&
        ["eligible", "already_reviewed"].includes(
          item.target.reviewEligibility,
        ) &&
        reviewer !== undefined &&
        record !== null &&
        !record.humanReview.reviewers.some(
          (candidate) =>
            candidate.ref === reviewer.ref &&
            candidate.identityStatus === reviewer.identityStatus,
        ) &&
        (record.authorship.kind === "ai" ||
          record.authorship.kind === "mixed") &&
        (item.anchorResolution.state === "unique" ||
          item.anchorResolution.state === "document") &&
        targetIsContained
      );
    });
  const markReviewed = async (
    requestedTargets: readonly ProvenanceTarget[],
    allowPreviouslyReviewed = false,
  ): Promise<void> => {
    if (mutationLock.current) return;
    if (mutationBlockedReason !== undefined) {
      setError(mutationBlockedReason);
      return;
    }
    const records = requestedTargets.map(effective);
    if (requestedTargets.length === 0 || view.currentStructuredHeadSha256 === null) {
      return;
    }
    if (records.some((record) => record === null)) {
      setError(
        "The selected provenance changed. Reselect the passage before recording review.",
      );
      return;
    }
    mutationLock.current = true;
    setPendingIds(
      new Set(
        records.flatMap((record) =>
          record === null ? [] : [record.attestationId],
        ),
      ),
    );
    setError(null);
    setSuccess(null);
    try {
      if (mutationBarrier === undefined) {
        throw new Error("The editor is not ready to record review safely.");
      }
      await mutationBarrier.runWithSynchronizedDocument(
        async (synchronized) => {
          const refreshed = await provider.refresh();
          if (refreshed.state !== "ready") {
            throw new Error(refreshed.reason);
          }
          const refreshedTargets = [
            ...(refreshed.data.documentDefault === null
              ? []
              : [refreshed.data.documentDefault]),
            ...refreshed.data.spans,
          ];
          const refreshedItems = resolvePanelTargets(
            refreshed.data,
            editor,
          );
          if (
            refreshed.data.currentStructuredHeadSha256 === null ||
            synchronized.structuredHeadSha256 !==
              refreshed.data.currentStructuredHeadSha256
          ) {
            throw new Error(
              "The document changed. Provenance was refreshed; inspect the passage and try again.",
            );
          }
          const refreshedRecords = requestedTargets.map((target, index) => {
            const refreshedTarget = refreshedTargets.find(
              (candidate) =>
                candidate.projectionId === target.projectionId,
            );
            const refreshedRecord =
              refreshedTarget === undefined ? null : effective(refreshedTarget);
            const refreshedItem = refreshedItems.find(
              (candidate) =>
                candidate.target.projectionId === target.projectionId,
            );
            const expectedRecord = records[index];
            const eligible =
              refreshedTarget?.reviewEligibility === "eligible" ||
              (allowPreviouslyReviewed &&
                refreshedTarget?.reviewEligibility === "already_reviewed");
            const reviewerStillNeeded =
              !allowPreviouslyReviewed ||
              (selectionAction?.reviewer !== undefined &&
                !refreshedRecord?.humanReview.reviewers.some(
                  (candidate) =>
                    candidate.ref === selectionAction.reviewer?.ref &&
                    candidate.identityStatus ===
                      selectionAction.reviewer?.identityStatus,
                ));
            if (
              refreshedTarget === undefined ||
              !eligible ||
              !reviewerStillNeeded ||
              refreshedRecord?.attestationId !==
                expectedRecord?.attestationId ||
              refreshedItem === undefined ||
              refreshedItem.peerConflictIds.length > 0
            ) {
              throw new Error(
                "The document changed. Provenance was refreshed; inspect the passage and try again.",
              );
            }
            if (
              refreshedTarget.span !== null &&
              editor?.resolveTarget(refreshedTarget.span).state !== "unique"
            ) {
              throw new Error(
                "A selected passage no longer resolves to one location.",
              );
            }
            return refreshedRecord;
          });
          if (refreshedRecords.some((record) => record === null)) {
            throw new Error(
              "The selected provenance is no longer available for review.",
            );
          }
          const refreshedAttestationIds = refreshedRecords.flatMap((record) =>
            record === null ? [] : [record.attestationId],
          );
          if (
            allowPreviouslyReviewed &&
            selectionAction?.reviewer !== undefined
          ) {
            await provider.markReviewed(
              refreshedAttestationIds,
              refreshed.data.currentStructuredHeadSha256,
              selectionAction.reviewer,
            );
          } else {
            await provider.markReviewed(
              refreshedAttestationIds,
              refreshed.data.currentStructuredHeadSha256,
            );
          }
          if (
            allowPreviouslyReviewed &&
            selectionReviewFingerprint !== null
          ) {
            setCompletedSelectionFingerprint(selectionReviewFingerprint);
          }
          setSuccess(
            requestedTargets.length === 1
              ? `Your review was recorded for “${requestedTargets[0]!.span === null ? "the whole document" : passageExcerpt(requestedTargets[0]!.span.exact)}”.`
              : `Your review was recorded for ${String(requestedTargets.length)} selected passages.`,
          );
        },
      );
    } catch (cause) {
      setError(mutationErrorMessage(cause));
    } finally {
      mutationLock.current = false;
      setPendingIds(new Set());
    }
  };
  const selectionReviewEnabled =
    selectionReviewTargets.length > 0 &&
    selectionReviewTargetsAreCurrent &&
    !locallyDirty &&
    view.currentStructuredHeadSha256 !== null &&
    readOnly !== true &&
    mutationBlockedReason === undefined &&
    mutationBarrier !== undefined &&
    pendingIds.size === 0;
  const selectionReviewDisabledReason = selectionReviewEnabled
    ? null
    : pendingIds.size > 0
      ? "Human review is being recorded."
      : !selectionReviewTargetsAreCurrent
        ? "The selected provenance changed. Reselect the passage to review its current record."
        : locallyDirty
          ? "Synchronize local edits and refresh provenance before recording review."
          : view.currentStructuredHeadSha256 === null
            ? "No current structured document head is available, so review cannot be recorded."
            : readOnly === true
              ? "This document is read-only, so review cannot be recorded."
              : mutationBlockedReason !== undefined
                ? mutationBlockedReason
                : mutationBarrier === undefined
                  ? "The editor is not ready to record review safely."
                  : "Review cannot be recorded for this selection.";
  const selectionReviewReasonId = "wb-cowork-selection-review-disabled";
  const mutationBlockedReasonId = "wb-cowork-provenance-mutation-blocked";
  const selectionReviewDescriptionId = selectionReviewEnabled
    ? undefined
    : mutationBlockedReason !== undefined &&
        selectionReviewDisabledReason === mutationBlockedReason
      ? mutationBlockedReasonId
      : selectionReviewReasonId;
  return (
    <>
      <dl
        className="wb-cowork-provenance-panel__summary"
        aria-label="Provenance summary"
      >
        <div>
          <dt>Needs review</dt>
          <dd>{needsReviewCount}</dd>
        </div>
        <div>
          <dt>Reviewed</dt>
          <dd>{reviewedCount}</dd>
        </div>
        <div>
          <dt>Issues</dt>
          <dd>{issueCount}</dd>
        </div>
        <div>
          <dt>Unrecorded</dt>
          <dd>
            {inputProvenancePending ? "Updating…" : unrecorded ? "Yes" : "No"}
          </dd>
        </div>
      </dl>
      <div
        className="wb-cowork-provenance-panel__filters"
        aria-label="Filter provenance"
        role="group"
      >
        {(["all", "needs_review", "ai_authored", "issues"] as const).map(
          (item) => (
            <button
              key={item}
              type="button"
              aria-pressed={filter === item}
              onClick={() => setFilter(item)}
            >
              {item
                .replace(/_/gu, " ")
                .replace(/^./u, (letter: string) => letter.toUpperCase())}
            </button>
          ),
        )}
      </div>
      {error === null ? null : (
        <p className="wb-cowork-provenance-panel__error" role="alert">
          {error}
        </p>
      )}
      {inputProvenancePending ? (
        <p className="wb-cowork-provenance-panel__reason" role="status">
          Recent typing provenance is awaiting confirmation. The editor has
          captured the text, and other provenance actions remain available while
          authorship and review catch up.
        </p>
      ) : locallyDirty ? (
        <p className="wb-cowork-provenance-panel__reason" role="status">
          Local edits are not yet represented by the latest provenance snapshot.
          Exact passages are reanchored for inspection; recording review waits
          for synchronization.
        </p>
      ) : null}
      {mutationBlockedReason === undefined ? null : (
        <p
          id={mutationBlockedReasonId}
          className="wb-cowork-provenance-panel__reason"
          role="status"
        >
          {mutationBlockedReason}
        </p>
      )}
      <p className="wb-cowork-provenance-panel__success" aria-live="polite">
        {success ?? ""}
      </p>
      {selectionReviewItems.length === 0 ? null : (
        <article
          ref={routedSelectionReviewCardRef}
          className="wb-cowork-provenance-panel__selection-review"
          tabIndex={-1}
          aria-label="Review selected provenance"
        >
          <h3>
            {selectionAction?.targetIds.length === 1
              ? "Review selected passage"
              : `Review ${String(selectionAction?.targetIds.length ?? 0)} selected passages`}
          </h3>
          <p>
            Records your review for the eligible AI or mixed-authored
            provenance fully contained in the selection. It does not change
            authorship or assert that the text is true.
          </p>
          <button
            ref={routedSelectionReviewRef}
            type="button"
            disabled={!selectionReviewEnabled}
            aria-describedby={selectionReviewDescriptionId}
            onClick={() =>
              void markReviewed(selectionReviewTargets, true)
            }
          >
            {pendingIds.size > 0 ? "Recording…" : "Mark as reviewed"}
          </button>
          {selectionReviewDisabledReason === null ||
          selectionReviewDescriptionId === mutationBlockedReasonId ? null : (
            <p
              id={selectionReviewReasonId}
              className="wb-cowork-provenance-panel__reason"
            >
              {selectionReviewDisabledReason}
            </p>
          )}
        </article>
      )}
      {noRecordedProvenance && filter === "all" ? (
        <article className="wb-cowork-provenance-panel__unrecorded">
          <h3>No provenance has been recorded for this document</h3>
          <p>
            New typing is recorded automatically. Select existing text to record
            who wrote it and whether it was reviewed; its earlier source will
            remain untracked.
          </p>
        </article>
      ) : !inputProvenancePending && unrecorded && filter === "all" ? (
        <article className="wb-cowork-provenance-panel__unrecorded">
          <h3>Some text has no current provenance record</h3>
          <p>
            Unrecorded text is shown with the unknown pattern. It is not assumed
            to be human-authored.
          </p>
        </article>
      ) : null}
      {!hasText ? (
        <p className="wb-cowork-provenance-panel__empty">
          There is no text to map yet.
        </p>
      ) : null}
      {visible.length === 0 ? (
        !inputProvenancePending &&
        hasText &&
        (!noRecordedProvenance || filter !== "all") ? (
          <p className="wb-cowork-provenance-panel__empty">
            No provenance records match this filter.
          </p>
        ) : null
      ) : (
        <ol className="wb-cowork-provenance-panel__list">
          {visible.map(
            ({ target, anchorResolution, peerConflictIds }, index) => {
              const id = targetId(target);
              const record = effective(target);
              const quote =
                target.span !== null
                  ? passageExcerpt(target.span.exact)
                  : target.target.kind === "document_version"
                    ? "Whole document"
                    : "Unavailable passage";
              const active = selectedId === id;
              return (
                <li
                  key={`${id}:${String(index)}`}
                  data-state={
                    peerConflictIds.length > 0 ||
                    target.resolution === "conflicted" ||
                    target.target.currentness !== "current"
                      ? "issue"
                      : "recorded"
                  }
                >
                  <button
                    type="button"
                    ref={
                      selectionAction?.targetIds[0] === id
                        ? routedRowRef
                        : undefined
                    }
                    className="wb-cowork-provenance-panel__item"
                    aria-expanded={active}
                    onClick={() => {
                      setSelectedId(active ? null : id);
                      if (anchorResolution.state === "unique") {
                        editor?.focusTarget(id);
                      }
                    }}
                  >
                    <span className="wb-cowork-provenance-panel__quote">
                      {quote}
                    </span>
                    <span className="wb-cowork-provenance-panel__status">
                      {peerConflictIds.length > 0
                        ? "Conflicts with overlapping passage"
                        : locallyDirty &&
                            target.target.currentness === "current"
                          ? target.span === null
                            ? "Awaiting provenance refresh"
                            : anchorResolution.state === "unique"
                              ? "Reanchored for inspection"
                              : statusLabel(target, anchorResolution.state)
                          : statusLabel(target, anchorResolution.state)}
                    </span>
                  </button>
                  {active ? (
                    <div className="wb-cowork-provenance-panel__detail">
                      <ProvenanceDetails target={target} />
                      {peerConflictIds.length > 0 ? (
                        <div className="wb-cowork-provenance-panel__conflict">
                          <p className="wb-cowork-provenance-panel__reason">
                            Overlapping provenance records disagree on
                            authorship, review, or source. Review recording is
                            blocked until that conflict is resolved.
                          </p>
                          <ul>
                            {peerConflictIds.map((peerId) => {
                              const peer = targets.find(
                                (candidate) =>
                                  candidate.target.projectionId === peerId,
                              )?.target;
                              return peer === undefined ? null : (
                                <li key={peerId}>
                                  <strong>
                                    {peer.span !== null
                                      ? passageExcerpt(peer.span.exact)
                                      : peer.target.kind === "document_version"
                                        ? "Whole document"
                                        : "Unavailable passage"}
                                  </strong>
                                  {peer.effectiveAttestations.map((leaf) => (
                                    <RecordDetails
                                      key={leaf.attestationId}
                                      record={leaf}
                                    />
                                  ))}
                                </li>
                              );
                            })}
                          </ul>
                        </div>
                      ) : null}
                      {anchorResolution.state === "unavailable" ? (
                        <p className="wb-cowork-provenance-panel__reason">
                          The recorded passage target is unavailable. Its
                          append-only history remains inspectable, but it cannot
                          be shown or reviewed.
                        </p>
                      ) : anchorResolution.state === "missing" ? (
                        <p className="wb-cowork-provenance-panel__reason">
                          The recorded passage is no longer present in this
                          document.
                        </p>
                      ) : anchorResolution.state === "ambiguous" ? (
                        <p className="wb-cowork-provenance-panel__reason">
                          The recorded passage matches multiple locations, so
                          Co-work will not choose one.
                        </p>
                      ) : null}
                      {target.span !== null &&
                      anchorResolution.state === "unique" ? (
                        <button
                          type="button"
                          onClick={() => editor?.revealTarget(id)}
                        >
                          Show in document
                        </button>
                      ) : null}
                      {record !== null &&
                      (record.authorship.kind === "ai" ||
                        record.authorship.kind === "mixed")
                        ? (() => {
                            const targetCanReview = canReview(
                              target,
                              anchorResolution.state,
                              peerConflictIds.length > 0,
                              locallyDirty,
                            );
                            const reviewEnabled =
                              targetCanReview &&
                              view.currentStructuredHeadSha256 !== null &&
                              readOnly !== true &&
                              mutationBlockedReason === undefined &&
                              mutationBarrier !== undefined &&
                              pendingIds.size === 0;
                            const reason =
                              pendingIds.has(record.attestationId)
                                ? "Human review is being recorded."
                                : pendingIds.size > 0
                                  ? "Finish recording the current review before starting another."
                                  : peerConflictIds.length > 0
                                    ? "Overlapping provenance records disagree, so review cannot be recorded."
                                    : locallyDirty
                                      ? "Synchronize local edits and refresh provenance before recording review."
                                      : view.currentStructuredHeadSha256 ===
                                            null && targetCanReview
                                        ? "No current structured document head is available, so review cannot be recorded."
                                        : readOnly === true && targetCanReview
                                          ? "This document is read-only, so review cannot be recorded."
                                          : mutationBlockedReason !== undefined &&
                                              targetCanReview
                                            ? mutationBlockedReason
                                          : mutationBarrier === undefined &&
                                              targetCanReview
                                            ? "The editor is not ready to record review safely."
                                            : reviewBlockedReason(target);
                            const reasonId = `wb-cowork-provenance-review-reason-${id}`;
                            const descriptionId = reviewEnabled
                              ? undefined
                              : mutationBlockedReason !== undefined &&
                                  targetCanReview &&
                                  reason === mutationBlockedReason
                                ? mutationBlockedReasonId
                                : reasonId;
                            return (
                              <div className="wb-cowork-provenance-panel__actions">
                                <h4>Actions</h4>
                                <button
                                  type="button"
                                  disabled={!reviewEnabled}
                                  aria-describedby={descriptionId}
                                  onClick={() => void markReviewed([target])}
                                >
                                  {pendingIds.has(record.attestationId)
                                    ? "Recording…"
                                    : "Mark reviewed"}
                                </button>
                                {reviewEnabled ? (
                                  <p className="wb-cowork-provenance-panel__reason">
                                    Records that you reviewed this text. It does
                                    not change its authorship or assert that it
                                    is true.
                                  </p>
                                ) : descriptionId === reasonId ? (
                                  <p
                                    id={reasonId}
                                    className="wb-cowork-provenance-panel__reason"
                                  >
                                    {reason}
                                  </p>
                                ) : null}
                              </div>
                            );
                          })()
                        : null}
                    </div>
                  ) : null}
                </li>
              );
            },
          )}
        </ol>
      )}
      <details className="wb-cowork-provenance-panel__global-history">
        <summary>Complete provenance history ({view.history.length})</summary>
        {view.history.length === 0 ? (
          <p>No immutable provenance records have been recorded.</p>
        ) : (
          <ol>
            {view.history.map((entry) => (
              <li key={entry.attestationId}>
                <RecordDetails record={entry} />
                <p>
                  {entry.supersedesId === null
                    ? "Original append-only entry"
                    : "Supersedes a prior append-only entry"}
                </p>
              </li>
            ))}
          </ol>
        )}
      </details>
      {selected === null ? null : (
        <span className="wb-visually-hidden">Provenance target selected</span>
      )}
    </>
  );
}

export function ProvenancePanel(props: ProvenancePanelProps) {
  const [data, setData] = useState<ProvenanceLoad | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const requestSequence = useRef(0);
  const load = useCallback((): void => {
    const sequence = ++requestSequence.current;
    void props.provider.load().then(
      (next) => {
        if (sequence === requestSequence.current) {
          setData(next);
          setError(null);
        }
      },
      (cause) => {
        if (sequence === requestSequence.current) {
          setError(
            cause instanceof Error
              ? cause.message
              : "Provenance could not load.",
          );
        }
      },
    );
  }, [props.provider]);
  useEffect(() => {
    if (!props.active) return undefined;
    load();
    return undefined;
  }, [load, props.active, reload]);
  useEffect(() => {
    if (!props.active) return undefined;
    let subscribing = true;
    const unsubscribe = props.provider.subscribe(() => {
      // Some live providers replay their retained snapshot synchronously.
      // The explicit initial load below already consumes it; later callbacks
      // are genuine invalidations and reload without rebuilding the subscription.
      if (!subscribing) load();
    });
    subscribing = false;
    return () => {
      requestSequence.current += 1;
      unsubscribe();
    };
  }, [load, props.active, props.provider]);
  const content = useMemo(() => {
    if (error !== null && data === null)
      return (
        <div className="wb-cowork-provenance-panel__unavailable" role="alert">
          <h2>Provenance could not load</h2>
          <p>{error}</p>
          <button type="button" onClick={() => setReload((value) => value + 1)}>
            Retry
          </button>
        </div>
      );
    if (data === null)
      return (
        <p className="wb-cowork-provenance-panel__empty" role="status">
          Loading provenance…
        </p>
      );
    if (data.state === "unavailable")
      return (
        <div className="wb-cowork-provenance-panel__unavailable" role="status">
          <h2>Provenance view unavailable</h2>
          <p>{data.reason}</p>
          <button type="button" onClick={() => setReload((value) => value + 1)}>
            Retry
          </button>
        </div>
      );
    return (
      <>
        {error === null ? null : (
          <p className="wb-cowork-provenance-panel__error" role="alert">
            {error}{" "}
            <button
              type="button"
              onClick={() => setReload((value) => value + 1)}
            >
              Retry
            </button>
          </p>
        )}
        <ProvenanceContents
          load={data}
          provider={props.provider}
          editor={props.editor}
          readOnly={props.readOnly}
          mutationBarrier={props.mutationBarrier}
          selectionAction={props.selectionAction}
          inputProvenancePending={props.inputProvenancePending}
          mutationBlockedReason={props.mutationBlockedReason}
        />
      </>
    );
  }, [
    data,
    error,
    props.editor,
    props.inputProvenancePending,
    props.mutationBarrier,
    props.provider,
    props.readOnly,
    props.mutationBlockedReason,
    props.selectionAction,
  ]);
  return (
    <section
      className="wb-cowork-provenance-panel"
      aria-label="Document provenance"
      ref={props.scrollContainerRef}
    >
      {content}
    </section>
  );
}
