import type { ControlGraphSnapshot } from "./contracts";

interface ActionResult {
  readonly ok?: boolean;
  readonly detail?: string;
  readonly error?: string;
  readonly nodes?: ControlGraphSnapshot["nodes"];
  readonly cache?: ControlGraphSnapshot["cache"];
}

export interface SystemStatusClient {
  load(signal?: AbortSignal): Promise<ControlGraphSnapshot>;
  reprobe(): Promise<ControlGraphSnapshot>;
  setComponentWanted(
    componentId: string,
    wanted: boolean,
  ): Promise<ControlGraphSnapshot>;
  repair(
    requirementId: string,
    params?: Readonly<Record<string, unknown>>,
  ): Promise<ActionResult>;
  requestHelp(nodeId: string): Promise<ActionResult>;
}

async function readJson<T>(response: Response): Promise<T> {
  const payload = (await response.json()) as T & {
    readonly error?: string;
    readonly detail?: string;
  };
  if (!response.ok) {
    throw new Error(
      payload.error ?? payload.detail ?? `Request failed (${response.status})`,
    );
  }
  return payload;
}

async function postAction(
  path: string,
  body?: Readonly<Record<string, unknown>>,
): Promise<ActionResult> {
  const result = await readJson<ActionResult>(
    await fetch(path, {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    }),
  );
  if (result.ok === false) {
    throw new Error(result.detail ?? result.error ?? "The action did not complete.");
  }
  return result;
}

export const systemStatusClient: SystemStatusClient = {
  async load(signal) {
    return readJson<ControlGraphSnapshot>(
      await fetch("/api/control/graph", { signal }),
    );
  },

  async reprobe() {
    return readJson<ControlGraphSnapshot>(
      await fetch("/api/control/reprobe", { method: "POST" }),
    );
  },

  async setComponentWanted(componentId, wanted) {
    return readJson<ControlGraphSnapshot>(
      await fetch("/api/control/preference", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          updates: {
            [componentId]: {
              wanted,
              reason: "Changed from React Settings",
            },
          },
        }),
      }),
    );
  },

  async repair(requirementId, params = {}) {
    return postAction(
      `/api/control/fix/${encodeURIComponent(requirementId.replace(/^req:/, ""))}`,
      { params },
    );
  },

  async requestHelp(nodeId) {
    return postAction(`/api/control/help/${encodeURIComponent(nodeId)}`);
  },
};
