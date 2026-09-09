import { rm, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { parseArgs } from "node:util";
import { fileURLToPath } from "node:url";

export const normalDashboardPort = 5127;
export const applications = Object.freeze({ cowork: "seeds/cowork.py", tasks: "seeds/tasks.py" });
export const scenarios = Object.freeze({ cowork: ["lifecycle", "truth-panel"], tasks: ["browse", "organization"] });
export const liveArtifactRoot = fileURLToPath(new URL("../../test-results/live", import.meta.url));

export function parseOptions(args) {
  const { values } = parseArgs({
    args,
    options: {
      app: { type: "string", default: "cowork" },
      scenario: { type: "string" },
      interactive: { type: "boolean", default: false },
      dev: { type: "boolean", default: false },
      build: { type: "boolean", default: false },
      "frontend-port": { type: "string" },
    },
  });
  if (!Object.hasOwn(applications, values.app)) {
    throw new Error(`Unknown app: ${values.app}. Available apps: ${Object.keys(applications).join(", ")}`);
  }
  values.scenario ??= scenarios[values.app][0];
  if (!scenarios[values.app].includes(values.scenario)) {
    throw new Error(`Unknown scenario for ${values.app}: ${values.scenario}`);
  }
  const frontendPort = values["frontend-port"] === undefined
    ? undefined
    : Number(values["frontend-port"]);
  if (frontendPort !== undefined && (
    !Number.isInteger(frontendPort) || frontendPort < 1024 || frontendPort > 65535 ||
    frontendPort === normalDashboardPort
  )) {
    throw new Error("--frontend-port must be an unprivileged port other than 5127");
  }
  const dev = !values.build && (values.dev || values.interactive);
  return {
    app: values.app,
    scenario: values.scenario,
    seeder: applications[values.app],
    interactive: values.interactive || dev,
    dev,
    frontendPort,
    mode: dev
      ? "dev server (source edits reload live)"
      : "production build (source edits require a restart)",
  };
}

export function frontendLaunch(viteBin, options, backendUrl, frontendPort, environment) {
  const backend = new URL(backendUrl);
  if (backend.hostname !== "127.0.0.1" || backend.protocol !== "http:" ||
      !backend.port || Number(backend.port) === normalDashboardPort) {
    throw new Error("the live frontend requires an isolated loopback backend, never 5127");
  }
  return {
    args: [viteBin, ...(options.dev ? [] : ["preview"]), "--host", "127.0.0.1",
      "--port", String(frontendPort), "--strictPort"],
    env: { ...environment, WB_DASHBOARD_PROXY_TARGET: backendUrl },
  };
}

export async function mintInteractiveUrl(frontendUrl, app, nonce) {
  const response = await fetch(`${frontendUrl}/api/_live/identity-bootstrap`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: frontendUrl,
      "X-WB-Live-Control": nonce,
    },
    body: JSON.stringify({ origin: frontendUrl }),
    signal: AbortSignal.timeout(10_000),
  });
  const grant = await response.json();
  if (!response.ok || grant.ok !== true || typeof grant.token !== "string" || !grant.token) {
    throw new Error(`isolated identity bootstrap failed (${response.status})`);
  }
  return `${frontendUrl}/app/${app}#wb-bootstrap=${encodeURIComponent(grant.token)}`;
}

export async function removeHarnessRoot(tempRoot) {
  const resolvedTemp = path.resolve(tempRoot);
  const expectedPrefix = `${path.resolve(os.tmpdir())}${path.sep}work-buddy-live-`;
  if (!resolvedTemp.startsWith(expectedPrefix)) {
    throw new Error(`refusing to remove unexpected path: ${resolvedTemp}`);
  }
  const marker = await stat(path.join(resolvedTemp, ".wb-live-harness"));
  if (!marker.isFile()) throw new Error("the harness marker must be a file");
  await rm(resolvedTemp, { recursive: true, force: false, maxRetries: 5, retryDelay: 200 });
}
