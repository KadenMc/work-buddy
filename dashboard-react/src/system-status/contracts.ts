export type ControlNodeKind =
  | "domain"
  | "subsystem"
  | "component"
  | "requirement"
  | "capability";

export type ControlEffectiveState =
  | "ok"
  | "degraded"
  | "blocked"
  | "disabled"
  | "unconfigured"
  | "unknown";

export type ControlPreference =
  | "wanted"
  | "unwanted"
  | "undecided"
  | "required";

export interface ControlGraphNode {
  readonly id: string;
  readonly kind: ControlNodeKind;
  readonly label: string;
  readonly description: string;
  readonly grouping_parents: readonly string[];
  readonly preference: ControlPreference | null;
  readonly effective_state: ControlEffectiveState;
  readonly component_id: string | null;
  readonly requirement_ids: readonly string[];
  readonly status_reason: string;
  readonly blocking_issues: readonly string[];
  readonly fix_kind:
    | "none"
    | "programmatic"
    | "input_required"
    | "agent_handoff";
  readonly fix_params: Readonly<Record<string, unknown>>;
  readonly fix_preview: string | null;
}

export interface ControlGraphSnapshot {
  readonly nodes: Readonly<Record<string, ControlGraphNode>>;
  readonly read_only?: boolean;
  readonly cache?: {
    readonly cached?: boolean;
    readonly built_at?: number;
    readonly age_seconds?: number;
    readonly node_count?: number;
  };
}

export type SystemStatusBucket =
  | "needs-setup"
  | "needs-attention"
  | "disabled"
  | "healthy";

export const SYSTEM_STATUS_BUCKET_LABELS: Record<SystemStatusBucket, string> = {
  "needs-setup": "Needs setup",
  "needs-attention": "Needs attention",
  disabled: "Disabled by you",
  healthy: "Healthy",
};

export function bucketControlNode(
  node: ControlGraphNode,
): SystemStatusBucket {
  if (node.preference === "unwanted" || node.effective_state === "disabled") {
    return "disabled";
  }
  if (node.effective_state === "unconfigured") {
    return "needs-setup";
  }
  if (
    node.effective_state === "degraded" ||
    node.effective_state === "blocked" ||
    node.effective_state === "unknown"
  ) {
    return "needs-attention";
  }
  return "healthy";
}

export function getStatusComponents(
  snapshot: ControlGraphSnapshot,
): readonly ControlGraphNode[] {
  return Object.values(snapshot.nodes)
    .filter((node) => node.kind === "component")
    .sort((left, right) => left.label.localeCompare(right.label));
}
