import { ArrowClockwise } from "@phosphor-icons/react/ArrowClockwise";
import { CheckCircle } from "@phosphor-icons/react/CheckCircle";
import { FirstAid } from "@phosphor-icons/react/FirstAid";
import { PauseCircle } from "@phosphor-icons/react/PauseCircle";
import { WarningCircle } from "@phosphor-icons/react/WarningCircle";
import { Wrench } from "@phosphor-icons/react/Wrench";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Button, InlineAlert, Spinner } from "../ui";
import { systemStatusClient, type SystemStatusClient } from "./client";
import {
  bucketControlNode,
  getStatusComponents,
  SYSTEM_STATUS_BUCKET_LABELS,
  type ControlGraphNode,
  type ControlGraphSnapshot,
  type SystemStatusBucket,
} from "./contracts";
import "./styles.css";

const BUCKET_ORDER: readonly SystemStatusBucket[] = [
  "needs-setup",
  "needs-attention",
  "disabled",
  "healthy",
];

const BUCKET_DESCRIPTIONS: Record<SystemStatusBucket, string> = {
  "needs-setup": "Components waiting for required configuration.",
  "needs-attention": "Components that are blocked, degraded, or could not be checked.",
  disabled: "Optional components you have chosen not to run.",
  healthy: "Configured components currently reporting a healthy state.",
};

function BucketIcon({ bucket }: { readonly bucket: SystemStatusBucket }) {
  if (bucket === "needs-setup") return <Wrench aria-hidden="true" />;
  if (bucket === "needs-attention") return <WarningCircle aria-hidden="true" />;
  if (bucket === "disabled") return <PauseCircle aria-hidden="true" />;
  return <CheckCircle aria-hidden="true" />;
}

function describeState(node: ControlGraphNode) {
  if (node.status_reason.trim()) return node.status_reason;
  if (node.blocking_issues.length) return node.blocking_issues.join(" ");
  return node.description || "No additional status detail is available.";
}

export function ControlStatusPage({
  client = systemStatusClient,
}: {
  readonly client?: SystemStatusClient;
}) {
  const [snapshot, setSnapshot] = useState<ControlGraphSnapshot | null>(null);
  const [selectedBucket, setSelectedBucket] =
    useState<SystemStatusBucket>("needs-setup");
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        setError(null);
        setSnapshot(await client.load(signal));
      } catch (caught) {
        if (signal?.aborted) return;
        setError(caught instanceof Error ? caught.message : "Status could not be loaded.");
      }
    },
    [client],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const components = useMemo(
    () => (snapshot ? getStatusComponents(snapshot) : []),
    [snapshot],
  );
  const isReadOnly = snapshot?.read_only === true;
  const groups = useMemo(
    (): Record<SystemStatusBucket, readonly ControlGraphNode[]> => ({
      "needs-setup": components.filter(
        (node) => bucketControlNode(node) === "needs-setup",
      ),
      "needs-attention": components.filter(
        (node) => bucketControlNode(node) === "needs-attention",
      ),
      disabled: components.filter(
        (node) => bucketControlNode(node) === "disabled",
      ),
      healthy: components.filter(
        (node) => bucketControlNode(node) === "healthy",
      ),
    }),
    [components],
  );

  useEffect(() => {
    if (!snapshot || groups[selectedBucket].length) return;
    const firstPopulated = BUCKET_ORDER.find((bucket) => groups[bucket].length);
    if (firstPopulated) setSelectedBucket(firstPopulated);
  }, [groups, selectedBucket, snapshot]);

  const run = useCallback(
    async (key: string, action: () => Promise<unknown>, message: string) => {
      setBusyAction(key);
      setError(null);
      setNotice(null);
      try {
        await action();
        setNotice(message);
        await load();
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "The action could not be completed.");
      } finally {
        setBusyAction(null);
      }
    },
    [load],
  );

  const recheck = useCallback(async () => {
    setBusyAction("reprobe");
    setError(null);
    setNotice(null);
    try {
      setSnapshot(await client.reprobe());
      setNotice("System checks are up to date.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Checks could not be refreshed.");
    } finally {
      setBusyAction(null);
    }
  }, [client]);

  if (!snapshot && !error) {
    return (
      <section className="wb-control-status wb-control-status--loading" aria-label="System status">
        <Spinner />
        <p>Checking Work Buddy components…</p>
      </section>
    );
  }

  return (
    <section className="wb-control-status" aria-labelledby="wb-control-status-title">
      <header className="wb-control-status__header">
        <div>
          <p className="wb-control-status__eyebrow">System</p>
          <h1 id="wb-control-status-title">Status &amp; repairs</h1>
          <p>
            See what needs setup, what is unhealthy, and what Work Buddy can repair.
            Ordinary preferences remain separate from these live system facts.
          </p>
        </div>
        <Button
          variant="secondary"
          size="small"
          disabled={busyAction !== null || isReadOnly}
          title={isReadOnly ? "Unavailable while this dashboard is read-only." : undefined}
          onClick={() => void recheck()}
        >
          <ArrowClockwise aria-hidden="true" />
          {busyAction === "reprobe" ? "Checking…" : "Recheck all"}
        </Button>
      </header>

      {error ? (
        <InlineAlert tone="danger" role="alert" aria-live="assertive">
          {error}
        </InlineAlert>
      ) : null}
      {notice ? (
        <InlineAlert tone="success" role="status" aria-live="polite">
          {notice}
        </InlineAlert>
      ) : null}
      {isReadOnly ? (
        <InlineAlert tone="info">
          This dashboard is read-only. You can inspect status, but checks,
          preferences, repairs, and help requests are unavailable here.
        </InlineAlert>
      ) : null}

      {snapshot ? (
        <>
          <div className="wb-control-status__summary" aria-label="Status summary">
            {BUCKET_ORDER.map((bucket) => (
              <button
                key={bucket}
                type="button"
                className="wb-control-status__summary-card"
                data-state={bucket}
                aria-label={`${SYSTEM_STATUS_BUCKET_LABELS[bucket]}: ${groups[bucket].length}`}
                aria-pressed={selectedBucket === bucket}
                onClick={() => setSelectedBucket(bucket)}
              >
                <BucketIcon bucket={bucket} />
                <span>
                  <strong>{groups[bucket].length}</strong>
                  <small>{SYSTEM_STATUS_BUCKET_LABELS[bucket]}</small>
                </span>
              </button>
            ))}
          </div>

          <div className="wb-control-status__group">
            <div className="wb-control-status__group-heading">
              <div>
                <h2>{SYSTEM_STATUS_BUCKET_LABELS[selectedBucket]}</h2>
                <p>{BUCKET_DESCRIPTIONS[selectedBucket]}</p>
              </div>
              <span>
                {groups[selectedBucket].length}{" "}
                {groups[selectedBucket].length === 1 ? "component" : "components"}
              </span>
            </div>

            {groups[selectedBucket].length ? (
              <ul className="wb-control-status__list">
                {groups[selectedBucket].map((node) => {
                  const relatedRequirements = node.requirement_ids
                    .map((id) => snapshot.nodes[id])
                    .filter(
                      (candidate): candidate is ControlGraphNode =>
                        Boolean(candidate) && candidate.effective_state !== "ok",
                    );
                  const isRequired = node.preference === "required";
                  const isDisabled = node.preference === "unwanted";
                  return (
                    <li key={node.id} className="wb-control-status__component">
                      <div className="wb-control-status__component-main">
                        <span
                          className="wb-control-status__state-dot"
                          data-state={bucketControlNode(node)}
                          aria-hidden="true"
                        />
                        <div>
                          <div className="wb-control-status__component-title">
                            <h3>{node.label}</h3>
                            {isRequired ? <span>Required</span> : null}
                          </div>
                          <p>{describeState(node)}</p>
                        </div>
                      </div>

                      <div className="wb-control-status__actions">
                        {!isRequired && node.component_id ? (
                          <Button
                            size="small"
                            variant="ghost"
                            disabled={busyAction !== null || isReadOnly}
                            title={isReadOnly ? "Unavailable while this dashboard is read-only." : undefined}
                            onClick={() =>
                              void run(
                                `preference:${node.id}`,
                                () =>
                                  client.setComponentWanted(
                                    node.component_id!,
                                    isDisabled,
                                  ),
                                isDisabled
                                  ? `${node.label} is enabled.`
                                  : `${node.label} is disabled.`,
                              )
                            }
                          >
                            {busyAction === `preference:${node.id}`
                              ? "Saving…"
                              : isDisabled
                                ? "Enable"
                                : "Disable"}
                          </Button>
                        ) : null}
                        {node.effective_state !== "ok" && !isDisabled ? (
                          <Button
                            size="small"
                            variant="secondary"
                            disabled={busyAction !== null || isReadOnly}
                            title={isReadOnly ? "Unavailable while this dashboard is read-only." : undefined}
                            onClick={() =>
                              void run(
                                `help:${node.id}`,
                                () => client.requestHelp(node.id),
                                `Help is being prepared for ${node.label}.`,
                              )
                            }
                          >
                            <FirstAid aria-hidden="true" />
                            {busyAction === `help:${node.id}` ? "Starting…" : "Get help"}
                          </Button>
                        ) : null}
                      </div>

                      {relatedRequirements.length ? (
                        <ul className="wb-control-status__requirements" aria-label={`${node.label} setup checks`}>
                          {relatedRequirements.map((requirement) => (
                            <li key={requirement.id}>
                              <div>
                                <strong>{requirement.label}</strong>
                                <span>{describeState(requirement)}</span>
                              </div>
                              {!isDisabled && requirement.fix_kind !== "none" ? (
                                <Button
                                  size="small"
                                  variant="primary"
                                  disabled={
                                    busyAction !== null ||
                                    isReadOnly ||
                                    requirement.fix_kind === "input_required"
                                  }
                                  title={
                                    isReadOnly
                                      ? "Unavailable while this dashboard is read-only."
                                      : requirement.fix_kind === "input_required"
                                      ? "This repair needs more information. Open Get help for guided next steps."
                                      : requirement.fix_preview ?? undefined
                                  }
                                  onClick={() =>
                                    void run(
                                      `repair:${requirement.id}`,
                                      () => client.repair(requirement.id, requirement.fix_params),
                                      `${requirement.label} repair finished.`,
                                    )
                                  }
                                >
                                  <Wrench aria-hidden="true" />
                                  {busyAction === `repair:${requirement.id}` ? "Repairing…" : "Repair"}
                                </Button>
                              ) : null}
                            </li>
                          ))}
                        </ul>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="wb-control-status__empty">Nothing is in this category.</p>
            )}
          </div>
        </>
      ) : (
        <Button variant="secondary" onClick={() => void load()}>
          Try again
        </Button>
      )}
    </section>
  );
}
