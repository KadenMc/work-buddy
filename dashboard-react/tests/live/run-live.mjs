import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { once } from "node:events";
import {
  mkdir,
  mkdtemp,
  readFile,
  stat,
  writeFile,
} from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as Y from "yjs";
import {
  frontendLaunch,
  liveArtifactRoot as artifactRoot,
  mintInteractiveUrl,
  normalDashboardPort,
  parseOptions,
  removeHarnessRoot,
} from "./harness.mjs";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const dashboardRoot = path.resolve(scriptDirectory, "../..");
const repoRoot = path.resolve(dashboardRoot, "..");
const options = parseOptions(process.argv.slice(2));
const { interactive } = options;
const commandEvidence = [];
const children = [];
const logs = new Map();

const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");

const freePort = () =>
  new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (address === null || typeof address === "string") {
        server.close();
        reject(new Error("could not allocate a loopback port"));
        return;
      }
      const allocated = address.port;
      server.close((error) => (error ? reject(error) : resolve(allocated)));
    });
  });

const waitForUrl = async (url, predicate, label) => {
  const deadline = Date.now() + 120_000;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(3_000) });
      if (await predicate(response)) return;
      lastError = new Error(`${label} returned ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`${label} did not become ready: ${String(lastError)}`);
};

const redactor = (tempRoot, hostRoot) => {
  const replacements = [
    [tempRoot, "<TEMP_ROOT>"],
    [tempRoot.replaceAll("\\", "/"), "<TEMP_ROOT>"],
    [hostRoot, "<HOST_ROOT>"],
    [hostRoot.replaceAll("\\", "/"), "<HOST_ROOT>"],
  ];
  return (value) => {
    let output = String(value).replace(/\u001b\[[0-9;]*m/g, "");
    for (const [needle, replacement] of replacements) {
      output = output.split(needle).join(replacement);
    }
    return output;
  };
};

const launch = (label, executable, args, options, redact) => {
  const child = spawn(executable, args, {
    ...options,
    shell: false,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  children.push({ label, child });
  logs.set(label, []);
  const capture = (chunk) => {
    const text = redact(chunk.toString("utf-8"));
    logs.get(label).push(text);
  };
  child.stdout.on("data", capture);
  child.stderr.on("data", capture);
  commandEvidence.push({
    label,
    command: [executable, ...args].map((part) => redact(part)).join(" "),
  });
  return child;
};

const run = async (label, executable, args, options, redact) => {
  const child = launch(label, executable, args, options, redact);
  const [code, signal] = await once(child, "exit");
  const evidence = commandEvidence.findLast((item) => item.label === label);
  evidence.exit_code = code;
  evidence.signal = signal;
  if (code !== 0) {
    throw new Error(`${label} exited with ${code ?? signal}`);
  }
};

const waitForInteractiveWindow = (durationMs) =>
  new Promise((resolve) => {
    let settled = false;
    const finish = (reason) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      process.off("SIGINT", onInterrupt);
      process.off("SIGTERM", onTerminate);
      resolve(reason);
    };
    const onInterrupt = () => finish("sigint");
    const onTerminate = () => finish("sigterm");
    const timer = setTimeout(() => finish("ttl_expired"), durationMs);
    process.once("SIGINT", onInterrupt);
    process.once("SIGTERM", onTerminate);
  });

const stopChild = async ({ child }) => {
  if (child.exitCode !== null || child.signalCode !== null) return;
  if (process.platform === "win32") {
    const killer = spawn("taskkill.exe", ["/PID", String(child.pid), "/T", "/F"], {
      shell: false,
      windowsHide: true,
      stdio: "ignore",
    });
    await Promise.race([
      once(killer, "exit"),
      new Promise((resolve) => setTimeout(resolve, 10_000)),
    ]);
  } else {
    child.kill("SIGTERM");
  }
  await Promise.race([
    once(child, "exit"),
    new Promise((resolve) => setTimeout(resolve, 10_000)),
  ]);
  if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
};

let tempRoot;
let hostRoot;
let fixture;
let exitCode = 1;
let cleanupSucceeded = false;
let failure;
let interactiveStopReason;
let tearingDown = false;
let interactiveSession;

try {
  await mkdir(artifactRoot, { recursive: true });
  tempRoot = await mkdtemp(path.join(os.tmpdir(), "work-buddy-live-"));
  hostRoot = path.join(tempRoot, "host-folders");
  const dataRoot = path.join(tempRoot, "data");
  const configRoot = path.join(tempRoot, "config");
  const fixtureFile = path.join(tempRoot, "fixture-manifest.json");
  const marker = path.join(tempRoot, ".wb-live-harness");
  await mkdir(hostRoot, { recursive: true });
  await mkdir(dataRoot, { recursive: true });
  await mkdir(configRoot, { recursive: true });
  await writeFile(marker, "wb-live-harness/v1\n", "utf-8");

  let backendPort = await freePort();
  while (backendPort === normalDashboardPort || backendPort === options.frontendPort) {
    backendPort = await freePort();
  }
  let frontendPort = options.frontendPort ?? await freePort();
  while (frontendPort === normalDashboardPort || frontendPort === backendPort) {
    frontendPort = await freePort();
  }
  const nonce = randomUUID();
  const backendUrl = `http://127.0.0.1:${backendPort}`;
  const frontendUrl = `http://127.0.0.1:${frontendPort}`;
  const config = {
    vault_root: hostRoot,
    repos_root: hostRoot,
    paths: { data_root: dataRoot },
    tasks: { db_path: path.join(dataRoot, "db", "tasks.db") },
    projects: { db_path: path.join(dataRoot, "db", "projects.db") },
    dashboard: {
      read_only: false,
      cowork_allowed_roots: [hostRoot],
    },
    sidecar: {
      services: {
        dashboard: {
          module: "work_buddy.dashboard",
          host: "127.0.0.1",
          port: backendPort,
          enabled: true,
        },
      },
    },
  };
  await writeFile(
    path.join(configRoot, "config.yaml"),
    `${JSON.stringify(config, null, 2)}\n`,
    "utf-8",
  );

  const redact = redactor(tempRoot, hostRoot);
  const isolatedEnv = {
    ...process.env,
    WORK_BUDDY_SESSION_ID: `wb-live-${nonce}`,
    WORK_BUDDY_CONFIG_DIR: configRoot,
    WORK_BUDDY_DATA_DIR: dataRoot,
    WORK_BUDDY_ASSET_ROOT: repoRoot,
    WB_LIVE_ROOT: tempRoot,
    WB_LIVE_HOST_ROOT: hostRoot,
    WB_LIVE_FIXTURE_FILE: fixtureFile,
    WB_LIVE_BACKEND_PORT: String(backendPort),
    WB_LIVE_HARNESS_NONCE: nonce,
    WB_LIVE_SCENARIO: options.scenario,
    WB_LIVE_APP: options.app,
  };

  await run(
    "seed",
    "uv",
    [
      "run",
      "--no-sync",
      "python",
      path.join("dashboard-react", "tests", "live", options.seeder),
    ],
    { cwd: repoRoot, env: isolatedEnv },
    redact,
  );

  fixture = JSON.parse(await readFile(fixtureFile, "utf-8"));
  if (options.app === "cowork") {
  const scratchDocument = new Y.Doc();
  const paragraph = new Y.XmlElement("paragraph");
  const scratchMarker = "Recovered scratch marker - exact local writing.";
  paragraph.insert(0, [new Y.XmlText(scratchMarker)]);
  scratchDocument.getXmlFragment("default").insert(0, [paragraph]);
  const scratchSnapshot = Y.encodeStateAsUpdate(scratchDocument);
  scratchDocument.destroy();
  fixture.scratch = {
    id: "cowork-empty",
    marker: scratchMarker,
    snapshot_base64: Buffer.from(scratchSnapshot).toString("base64"),
    snapshot_sha256: sha256(scratchSnapshot),
  };
  }
  fixture.harness = {
    backend_port: backendPort,
    frontend_port: frontendPort,
    normal_dashboard_port: normalDashboardPort,
    nonce,
  };
  await writeFile(fixtureFile, `${JSON.stringify(fixture, null, 2)}\n`, "utf-8");

  process.stdout.write(`Environment: isolated live harness; app: ${options.app}; mode: ${options.mode}\n`);

  if (!options.dev && process.env.WB_LIVE_SKIP_BUILD !== "1") {
    const typescriptCli = path.join(
      dashboardRoot,
      "node_modules",
      "typescript",
      "bin",
      "tsc",
    );
    await run(
      "production-typecheck",
      process.execPath,
      [typescriptCli, "--noEmit"],
      { cwd: dashboardRoot, env: process.env },
      redact,
    );
    const viteBuildCli = path.join(
      dashboardRoot,
      "node_modules",
      "vite",
      "bin",
      "vite.js",
    );
    await run(
      "production-build",
      process.execPath,
      [viteBuildCli, "build"],
      { cwd: dashboardRoot, env: process.env },
      redact,
    );
  } else if (!options.dev) {
    await stat(path.join(dashboardRoot, "dist", "index.html"));
  }

  const backend = launch(
    "flask-backend",
    "uv",
    [
      "run",
      "--no-sync",
      "python",
      path.join("dashboard-react", "tests", "live", "live_server.py"),
    ],
    { cwd: repoRoot, env: isolatedEnv },
    redact,
  );
  backend.once("exit", (code) => {
    if (!tearingDown && code !== null && code !== 0) {
      failure ??= new Error(`Flask exited early (${code})`);
    }
  });
  await waitForUrl(
    `${backendUrl}/health`,
    (response) =>
      response.ok && response.headers.get("x-wb-live-harness") === nonce,
    "isolated Flask backend",
  );

  const viteBin = path.join(dashboardRoot, "node_modules", "vite", "bin", "vite.js");
  const frontend = frontendLaunch(viteBin, options, backendUrl, frontendPort, process.env);
  const preview = launch(
    options.dev ? "vite-dev" : "vite-preview",
    process.execPath,
    frontend.args,
    {
      cwd: dashboardRoot,
      env: frontend.env,
    },
    redact,
  );
  preview.once("exit", (code) => {
    if (!tearingDown && code !== null && code !== 0) {
      failure ??= new Error(`Vite exited early (${code})`);
    }
  });
  await waitForUrl(
    `${frontendUrl}/app/`,
    (response) => response.ok,
    options.dev ? "dev server" : "production preview",
  );

  if (process.env.WB_LIVE_FORCE_FAILURE === "1") {
    throw new Error("forced harness failure after server startup");
  }

  if (interactive) {
    const configuredDuration = Number(process.env.WB_LIVE_INTERACTIVE_MS ?? "600000");
    const durationMs = Number.isFinite(configuredDuration)
      ? Math.max(60_000, Math.min(configuredDuration, 1_800_000))
      : 600_000;
    const authenticatedUrl = await mintInteractiveUrl(frontendUrl, options.app, nonce);
    interactiveSession = {
      format: "wb-live-interactive/v1",
      status: "ready",
      app: options.app,
      scenario: options.scenario,
      mode: options.mode,
      frontend_url: authenticatedUrl,
      backend_url: backendUrl,
      nonce,
      temp_root: tempRoot,
      data_root: dataRoot,
      config_root: configRoot,
      fixture_file: fixtureFile,
      expires_at: new Date(Date.now() + durationMs).toISOString(),
    };
    await writeFile(
      path.join(artifactRoot, "interactive-session.json"),
      `${JSON.stringify(
        interactiveSession,
        null,
        2,
      )}\n`,
      "utf-8",
    );
    process.stdout.write(
      `Isolated live authenticated URL: ${authenticatedUrl}\n` +
        `Session file: ${path.join(artifactRoot, "interactive-session.json")}\n` +
        `Automatic teardown in ${Math.round(durationMs / 1000)} seconds (Ctrl+C also tears down).\n`,
    );
    interactiveStopReason = await waitForInteractiveWindow(durationMs);
  } else {
    const playwrightCli = path.join(
      dashboardRoot,
      "node_modules",
      "@playwright",
      "test",
      "cli.js",
    );
    const playwrightArgs = [
      playwrightCli,
      "test",
      "--config",
      "playwright.live.config.ts",
    ];
    const grep = process.env.WB_LIVE_PLAYWRIGHT_GREP?.trim();
    const grepInvert = process.env.WB_LIVE_PLAYWRIGHT_GREP_INVERT?.trim();
    if (grep) playwrightArgs.push("--grep", grep);
    if (grepInvert) playwrightArgs.push("--grep-invert", grepInvert);
    await run(
      "playwright",
      process.execPath,
      playwrightArgs,
      {
        cwd: dashboardRoot,
        env: {
          ...process.env,
          WB_LIVE_APP: options.app,
          WB_LIVE_SCENARIO: options.scenario,
          WB_LIVE_BASE_URL: frontendUrl,
          WB_LIVE_BACKEND_URL: backendUrl,
          WB_LIVE_FIXTURE_FILE: fixtureFile,
          WB_LIVE_HARNESS_NONCE: nonce,
        },
      },
      redact,
    );
  }
  if (failure !== undefined) throw failure;
  exitCode = 0;
} catch (error) {
  failure = error;
  process.stderr.write(`${String(error?.stack ?? error)}\n`);
} finally {
  tearingDown = true;
  for (const child of children.reverse()) {
    const wasRunning = child.child.exitCode === null && child.child.signalCode === null;
    await stopChild(child).catch(() => undefined);
    const evidence = commandEvidence.findLast((item) => item.label === child.label);
    if (evidence !== undefined && evidence.exit_code === undefined) {
      evidence.exit_code = wasRunning ? null : child.child.exitCode;
      evidence.signal = wasRunning
        ? "terminated_after_run"
        : child.child.signalCode;
    }
  }
  if (tempRoot !== undefined) {
    try {
      await removeHarnessRoot(tempRoot);
      cleanupSucceeded = true;
    } catch (cleanupError) {
      failure ??= cleanupError;
      exitCode = 1;
    }
  }

  await mkdir(artifactRoot, { recursive: true });
  if (interactiveSession !== undefined) {
    await writeFile(path.join(artifactRoot, "interactive-session.json"),
      `${JSON.stringify({ ...interactiveSession, status: "stopped", cleanup_succeeded: cleanupSucceeded }, null, 2)}\n`,
      "utf-8");
  }
  for (const [label, entries] of logs) {
    await writeFile(path.join(artifactRoot, `${label}.log`), entries.join(""), "utf-8");
  }
  const summary = {
    format: "wb-live-evidence/v1",
    mode: options.mode,
    app: options.app,
    scenario: options.scenario,
    interactive,
    ok: exitCode === 0,
    cleanup_succeeded: cleanupSucceeded,
    interactive_stop_reason: interactiveStopReason ?? null,
    failure: failure === undefined ? null : String(failure?.message ?? failure),
    fixture: fixture === undefined
      ? null
      : {
          format: fixture.format,
          initialized_store_id: fixture.initialized?.store_id ?? null,
          source_sha256: fixture.source?.sha256 ?? null,
          sentinel_sha256: fixture.sentinel?.sha256 ?? null,
          scratch_snapshot_sha256: fixture.scratch?.snapshot_sha256 ?? null,
          task_count: fixture.tasks?.task_count ?? null,
          project_ids: fixture.tasks?.project_ids ?? null,
        },
    commands: commandEvidence,
  };
  await writeFile(
    path.join(artifactRoot, "run-summary.json"),
    `${JSON.stringify(summary, null, 2)}\n`,
    "utf-8",
  );
}

process.exitCode = exitCode;
