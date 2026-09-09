import { useCallback, useEffect, useMemo, useState } from "react";

import { asSettingsPageId } from "../dashboard/contributions/contracts";
import { Button, InlineAlert } from "../ui";

export const EMBEDDING_SETTINGS_PAGE_ID = asSettingsPageId(
  "wb.settings.system.embeddings",
);

interface LastRoute {
  readonly provider?: string;
  readonly provider_model?: string;
  readonly fallback?: boolean;
  readonly status?: string;
  readonly reason?: string;
  readonly at?: number;
  readonly error?: string | null;
  readonly provider_error?: string | null;
  readonly provider_error_kind?: string | null;
  readonly provider_error_hint?: string | null;
}

interface RoutingStatus {
  readonly requested_provider?: string;
  readonly provider_model?: string;
  readonly on_error?: string;
  readonly configuration_status?: string;
  readonly configuration_error?: string | null;
  readonly authority_error?: string | null;
  readonly cooldown_remaining_s?: number;
  readonly last_route?: LastRoute | null;
}

interface EmbeddingRuntimeStatus {
  readonly service_status?: string;
  readonly policy?: {
    readonly effective?: string;
    readonly configured?: string;
    readonly pending?: string | null;
    readonly apply_status?: string;
    readonly revision?: string;
    readonly running?: string | null;
    readonly authority_matches_runtime?: boolean | null;
  } | null;
  readonly document_model?: {
    readonly key?: string;
    readonly name?: string;
    readonly dims?: number;
    readonly load_status?: string;
    readonly loaded_locally?: boolean;
    readonly error?: string | null;
    readonly routing?: RoutingStatus | null;
  } | null;
  readonly lmstudio?: {
    readonly ok?: boolean;
    readonly detail?: string;
    readonly model_advertised?: boolean | null;
    readonly model_ids?: readonly string[];
  } | null;
  readonly settings_error?: string;
  readonly service_error?: string;
}

const POLICY_LABELS: Record<string, string> = {
  local: "Local only",
  "prefer-lmstudio": "Prefer LM Studio",
  "require-lmstudio": "Require LM Studio",
};

function policyLabel(value?: string | null): string {
  return value ? POLICY_LABELS[value] ?? value : "Unknown";
}

function routeLabel(route?: LastRoute | null): string {
  if (!route) return "No document batch recorded since restart";
  if (route.status === "error" || route.status === "unavailable") {
    return `Failed (${route.reason ?? "provider unavailable"})`;
  }
  if (route.provider === "lmstudio") return "LM Studio";
  if (route.provider === "local" && route.fallback) return "Local fallback";
  if (route.provider === "local") return "Local encoder";
  return route.provider ?? "Unknown";
}

function routeTime(at?: number): string | undefined {
  if (typeof at !== "number" || !Number.isFinite(at)) return undefined;
  return new Date(at * 1000).toLocaleString();
}

function remoteModelObservation(
  policy: string | undefined,
  lmstudio: EmbeddingRuntimeStatus["lmstudio"],
): string {
  if (policy === "local") return "Not checked (local-only policy)";
  if (lmstudio?.ok === false) return "Cannot inspect while LM Studio is unreachable";
  if (lmstudio?.model_advertised === true) return "Advertised now";
  if (lmstudio?.model_advertised === false) {
    return "Not advertised now (may load on demand)";
  }
  return "Not reported";
}

function localMemoryImpact(
  loadedLocally: boolean | undefined,
  policy: string | undefined,
): string {
  if (loadedLocally) return "~526 MB leaf-ir weight payload loaded";
  if (loadedLocally === false) {
    if (policy === "local") return "~526 MB weights not loaded yet";
    if (policy === "prefer-lmstudio" || policy === "require-lmstudio") {
      return "~526 MB local weight load currently avoided";
    }
    return "Weights not loaded; running policy not reported";
  }
  return "Not reported (~526 MB when leaf-ir loads)";
}

function advertisedModels(modelIds?: readonly string[]): string | undefined {
  if (!modelIds?.length) return undefined;
  const visible = modelIds.slice(0, 4);
  const remainder = modelIds.length - visible.length;
  return `${visible.join(", ")}${remainder > 0 ? `, +${remainder} more` : ""}`;
}

export function EmbeddingRuntimePanel() {
  const [runtime, setRuntime] = useState<EmbeddingRuntimeStatus>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(undefined);
    try {
      const response = await fetch("/api/embeddings/runtime", {
        headers: { Accept: "application/json" },
        signal,
      });
      if (!response.ok) {
        throw new Error(`Runtime status request failed (${response.status})`);
      }
      setRuntime((await response.json()) as EmbeddingRuntimeStatus);
    } catch (reason) {
      if (signal?.aborted) return;
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const model = runtime?.document_model;
  const routing = model?.routing;
  const route = routing?.last_route;
  const runningPolicy = runtime?.policy?.running ?? undefined;
  const authorityPolicy = runtime?.policy?.effective;
  const requiresRemote = runningPolicy === "require-lmstudio";
  const prefersRemote = runningPolicy === "prefer-lmstudio";
  const remoteReachable = runtime?.lmstudio?.ok === true;
  const remoteUnavailable = runtime?.lmstudio?.ok === false;
  const remoteModelAdvertised = runtime?.lmstudio?.model_advertised;
  const routeFailed = route?.status === "error" || route?.status === "unavailable";
  const activeCooldown = (routing?.cooldown_remaining_s ?? 0) > 0;
  const remoteRouteSucceeded = route?.provider === "lmstudio" && route?.status === "ok";
  const lastRouteAt = useMemo(() => routeTime(route?.at), [route?.at]);
  const availableModels = advertisedModels(runtime?.lmstudio?.model_ids);
  const nonProviderError = route?.error !== route?.provider_error
    ? route?.error
    : undefined;

  let stateAlert = null;
  if (runtime?.service_status === "unavailable") {
    stateAlert = (
      <InlineAlert tone="danger">
        The Embedding service is unavailable. Document vectors are not being produced;
        lexical search remains available where supported.
      </InlineAlert>
    );
  } else if (routing?.authority_error) {
    stateAlert = (
      <InlineAlert tone="danger">
        Settings authority could not be read when the Embedding service started.
        Document and passage embedding is forced to the fail-closed LM Studio route,
        so the local document encoder will not load. Fix the settings error and restart
        the service: {routing.authority_error}
      </InlineAlert>
    );
  } else if (
    routing?.configuration_status === "error" ||
    routing?.configuration_error
  ) {
    stateAlert = (
      <InlineAlert tone="danger">
        The remote document route is misconfigured: {routing.configuration_error ??
          "configuration validation failed"}. Require mode blocks document vectors;
        Prefer mode uses its intentional local fallback. Correct the configuration and
        restart the Embedding service.
      </InlineAlert>
    );
  } else if (requiresRemote && (remoteUnavailable || (routeFailed && activeCooldown))) {
    stateAlert = (
      <InlineAlert tone="danger">
        Dense document and passage features cannot produce vectors until LM Studio
        can serve the configured model. Index refreshes still commit lexical documents
        and resumable ledger progress, while dense completion stays pending; similarity
        features use their available non-dense fallback. Required-remote mode will not
        load the document encoder locally.
      </InlineAlert>
    );
  } else if (requiresRemote && routeFailed) {
    stateAlert = (
      <InlineAlert tone="warning">
        The last document batch failed through LM Studio. The current probe does not
        prove that embedding has recovered; a successful document batch is still
        needed to confirm the route. Required-remote mode will not load the document
        encoder locally.
      </InlineAlert>
    );
  } else if (prefersRemote && (remoteUnavailable || route?.fallback)) {
    stateAlert = (
      <InlineAlert tone="warning">
        LM Studio is unavailable or the last request fell back. Prefer mode may load
        the large document encoder in this process until the remote route recovers.
      </InlineAlert>
    );
  } else if (
    (requiresRemote || prefersRemote) &&
    remoteReachable &&
    (remoteModelAdvertised === true || remoteRouteSucceeded)
  ) {
    stateAlert = (
      <InlineAlert tone="success">
        {remoteRouteSucceeded
          ? "The last document batch completed through LM Studio."
          : "LM Studio is reachable and currently advertises the configured document model."}
      </InlineAlert>
    );
  } else if (
    (requiresRemote || prefersRemote) &&
    remoteReachable &&
    remoteModelAdvertised === false
  ) {
    stateAlert = (
      <InlineAlert tone="info">
        LM Studio is reachable, but the configured document model is not currently
        advertised. It may still load on demand; only an embedding request can confirm
        the route.
      </InlineAlert>
    );
  } else if ((requiresRemote || prefersRemote) && remoteReachable) {
    stateAlert = (
      <InlineAlert tone="warning">
        LM Studio is reachable, but availability of the configured document model
        was not reported. The dashboard cannot yet verify the complete route.
      </InlineAlert>
    );
  } else if (requiresRemote || prefersRemote) {
    stateAlert = (
      <InlineAlert tone="warning">
        LM Studio reachability was not reported. The dashboard cannot yet verify the
        active remote document-encoding route.
      </InlineAlert>
    );
  }

  return (
    <section className="wb-embedding-runtime" aria-labelledby="embedding-runtime-title">
      <header className="wb-embedding-runtime__header">
        <div>
          <p className="wb-settings-content__eyebrow">Live verification</p>
          <h2 id="embedding-runtime-title">Active document route</h2>
          <p>
            This is runtime evidence from the Embedding service, not merely the saved
            preference. Query-side models stay local in every mode.
          </p>
        </div>
        <Button
          variant="secondary"
          size="small"
          disabled={loading}
          onClick={() => void load()}
        >
          {loading ? "Checking…" : "Refresh status"}
        </Button>
      </header>

      {error ? (
        <InlineAlert tone="warning" role="alert">
          Runtime verification is unavailable: {error}
        </InlineAlert>
      ) : null}
      {!error && loading && !runtime ? (
        <InlineAlert tone="info" role="status">Checking the running route…</InlineAlert>
      ) : null}
      {runtime?.policy?.apply_status === "restart-required" ? (
        <InlineAlert tone="info">
          {runningPolicy
            ? `${policyLabel(runtime.policy.configured)} is saved, but the running service remains on ${policyLabel(runningPolicy)} until restart.`
            : `${policyLabel(runtime.policy.configured)} is saved and waiting for an Embedding service restart. The current running policy was not reported.`}
        </InlineAlert>
      ) : null}
      {runtime?.policy?.authority_matches_runtime === false ? (
        <InlineAlert tone="warning" role="status">
          Settings authority reports {policyLabel(authorityPolicy)}, but Embedding
          service health reports {policyLabel(runningPolicy)}. The running-service
          value is authoritative for current behavior; restart the service and refresh
          this check.
        </InlineAlert>
      ) : null}
      {stateAlert}

      {runtime ? (
        <dl className="wb-embedding-runtime__facts">
          <div>
            <dt>Embedding service</dt>
            <dd>{runtime.service_status ?? "Unknown"}</dd>
          </div>
          <div>
            <dt>Active policy</dt>
            <dd>{policyLabel(runningPolicy)}</dd>
          </div>
          <div>
            <dt>Settings effective policy</dt>
            <dd>{policyLabel(authorityPolicy)}</dd>
          </div>
          <div>
            <dt>Local document weights</dt>
            <dd>
              {model?.loaded_locally === true
                ? "Loaded in this process"
                : model?.loaded_locally === false
                  ? "Not loaded"
                  : "Not reported"}
            </dd>
          </div>
          <div>
            <dt>LM Studio</dt>
            <dd>
              {runtime.lmstudio == null
                ? runningPolicy === "local"
                  ? "Not used by the active policy"
                  : "Not reported"
                : runtime.lmstudio.ok
                  ? "Reachable"
                  : "Unavailable"}
            </dd>
          </div>
          <div>
            <dt>Configured provider model</dt>
            <dd><code>{routing?.provider_model ?? "Not reported"}</code></dd>
          </div>
          <div>
            <dt>Configured remote model observation</dt>
            <dd>{remoteModelObservation(runningPolicy, runtime.lmstudio)}</dd>
          </div>
          <div>
            <dt>Estimated local-memory impact</dt>
            <dd>{localMemoryImpact(model?.loaded_locally, runningPolicy)}</dd>
          </div>
          <div>
            <dt>Last document batch</dt>
            <dd>
              {routeLabel(route)}
              {lastRouteAt ? <small>{lastRouteAt}</small> : null}
            </dd>
          </div>
        </dl>
      ) : null}

      {routing?.cooldown_remaining_s && routing.cooldown_remaining_s > 0 ? (
        <p className="wb-embedding-runtime__detail">
          Remote retry cooldown: {Math.ceil(routing.cooldown_remaining_s)} seconds.
        </p>
      ) : null}
      {runtime?.lmstudio?.detail ? (
        <p className="wb-embedding-runtime__detail">{runtime.lmstudio.detail}</p>
      ) : null}
      {remoteModelAdvertised === false && availableModels ? (
        <p className="wb-embedding-runtime__detail">
          Models currently advertised by LM Studio: <code>{availableModels}</code>
        </p>
      ) : null}
      {route?.provider_error ? (
        <p className="wb-embedding-runtime__detail wb-embedding-runtime__detail--error">
          Remote provider{route.provider_error_kind
            ? ` (${route.provider_error_kind})`
            : ""}: {route.provider_error}
          {route.provider_error_hint ? ` ${route.provider_error_hint}` : ""}
        </p>
      ) : null}
      <p className="wb-embedding-runtime__detail">
        The ~526 MB figure estimates leaf-ir's model-weight payload, not a guaranteed
        change in process private commit. Runtime buffers vary, and allocator
        high-water commit can persist until the Embedding service restarts.
      </p>
      {nonProviderError || model?.error || runtime?.settings_error || runtime?.service_error ? (
        <p className="wb-embedding-runtime__detail wb-embedding-runtime__detail--error">
          {nonProviderError ?? model?.error ?? runtime?.settings_error ?? runtime?.service_error}
        </p>
      ) : null}
    </section>
  );
}
