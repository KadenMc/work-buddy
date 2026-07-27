import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { once } from "node:events";
import {
  mkdir,
  mkdtemp,
  readFile,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as Y from "yjs";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const dashboardRoot = path.resolve(scriptDirectory, "../..");
const repoRoot = path.resolve(dashboardRoot, "..");
const artifactRoot = path.join(dashboardRoot, "test-results", "cowork-live");
const normalDashboardPort = 5127;
const interactive = process.argv.includes("--interactive");
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

try {
  await mkdir(artifactRoot, { recursive: true });
  tempRoot = await mkdtemp(path.join(os.tmpdir(), "work-buddy-cowork-live-"));
  hostRoot = path.join(tempRoot, "host-folders");
  const dataRoot = path.join(tempRoot, "data");
  const configRoot = path.join(tempRoot, "config");
  const fixtureFile = path.join(tempRoot, "fixture-manifest.json");
  const marker = path.join(tempRoot, ".cowork-live-harness");
  await mkdir(hostRoot, { recursive: true });
  await mkdir(dataRoot, { recursive: true });
  await mkdir(configRoot, { recursive: true });
  await writeFile(marker, "cowork-live-harness/v1\n", "utf-8");

  let backendPort = await freePort();
  while (backendPort === normalDashboardPort) backendPort = await freePort();
  let frontendPort = await freePort();
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
    WORK_BUDDY_SESSION_ID: `cowork-live-${nonce}`,
    WORK_BUDDY_CONFIG_DIR: configRoot,
    WORK_BUDDY_DATA_DIR: dataRoot,
    WORK_BUDDY_ASSET_ROOT: repoRoot,
    COWORK_LIVE_ROOT: tempRoot,
    COWORK_LIVE_HOST_ROOT: hostRoot,
    COWORK_LIVE_FIXTURE_FILE: fixtureFile,
    COWORK_LIVE_BACKEND_PORT: String(backendPort),
    COWORK_LIVE_HARNESS_NONCE: nonce,
  };

  await run(
    "seed",
    "uv",
    [
      "run",
      "--no-sync",
      "python",
      path.join("dashboard-react", "tests", "live", "seed_cowork_live.py"),
    ],
    { cwd: repoRoot, env: isolatedEnv },
    redact,
  );

  fixture = JSON.parse(await readFile(fixtureFile, "utf-8"));
  const scratchDocument = new Y.Doc();
  const paragraph = new Y.XmlElement("paragraph");
  const scratchMarker = "Recovered scratch marker — exact local writing.";
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
  fixture.harness = {
    backend_port: backendPort,
    frontend_port: frontendPort,
    normal_dashboard_port: normalDashboardPort,
    nonce,
  };
  await writeFile(fixtureFile, `${JSON.stringify(fixture, null, 2)}\n`, "utf-8");

  if (process.env.COWORK_LIVE_SKIP_BUILD !== "1") {
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
  } else {
    await stat(path.join(dashboardRoot, "dist", "index.html"));
  }

  const backend = launch(
    "flask-backend",
    "uv",
    [
      "run",
      "--no-sync",
      "python",
      path.join("dashboard-react", "tests", "live", "cowork_live_server.py"),
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
      response.ok && response.headers.get("x-wb-cowork-live-harness") === nonce,
    "isolated Flask backend",
  );

  const viteBin = path.join(dashboardRoot, "node_modules", "vite", "bin", "vite.js");
  const preview = launch(
    "vite-preview",
    process.execPath,
    [viteBin, "preview", "--host", "127.0.0.1", "--port", String(frontendPort), "--strictPort"],
    {
      cwd: dashboardRoot,
      env: {
        ...process.env,
        WB_DASHBOARD_PROXY_TARGET: backendUrl,
      },
    },
    redact,
  );
  preview.once("exit", (code) => {
    if (!tearingDown && code !== null && code !== 0) {
      failure ??= new Error(`Vite preview exited early (${code})`);
    }
  });
  await waitForUrl(
    `${frontendUrl}/app/`,
    (response) => response.ok,
    "production preview",
  );

  if (process.env.COWORK_LIVE_FORCE_FAILURE === "1") {
    throw new Error("forced harness failure after server startup");
  }

  if (interactive) {
    const configuredDuration = Number(process.env.COWORK_LIVE_INTERACTIVE_MS ?? "600000");
    const durationMs = Number.isFinite(configuredDuration)
      ? Math.max(60_000, Math.min(configuredDuration, 1_800_000))
      : 600_000;
    await writeFile(
      path.join(artifactRoot, "interactive-session.json"),
      `${JSON.stringify(
        {
          format: "cowork-live-interactive/v1",
          status: "ready",
          frontend_url: `${frontendUrl}/app/cowork?mode=launcher`,
          backend_url: backendUrl,
          fixture_file: fixtureFile,
          expires_at: new Date(Date.now() + durationMs).toISOString(),
        },
        null,
        2,
      )}\n`,
      "utf-8",
    );
    process.stdout.write(
      `Isolated Co-work interactive URL: ${frontendUrl}/app/cowork?mode=launcher\n` +
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
      "playwright.cowork-live.config.ts",
    ];
    const grep = process.env.COWORK_LIVE_PLAYWRIGHT_GREP?.trim();
    const grepInvert = process.env.COWORK_LIVE_PLAYWRIGHT_GREP_INVERT?.trim();
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
          COWORK_LIVE_BASE_URL: frontendUrl,
          COWORK_LIVE_BACKEND_URL: backendUrl,
          COWORK_LIVE_FIXTURE_FILE: fixtureFile,
          COWORK_LIVE_HARNESS_NONCE: nonce,
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
    const resolvedTemp = path.resolve(tempRoot);
    const expectedPrefix = `${path.resolve(os.tmpdir())}${path.sep}work-buddy-cowork-live-`;
    try {
      if (!resolvedTemp.startsWith(expectedPrefix)) {
        throw new Error(`refusing to remove unexpected path: ${resolvedTemp}`);
      }
      await stat(path.join(resolvedTemp, ".cowork-live-harness"));
      await rm(resolvedTemp, { recursive: true, force: false, maxRetries: 5, retryDelay: 200 });
      cleanupSucceeded = true;
    } catch (cleanupError) {
      failure ??= cleanupError;
      exitCode = 1;
    }
  }

  await mkdir(artifactRoot, { recursive: true });
  for (const [label, entries] of logs) {
    await writeFile(path.join(artifactRoot, `${label}.log`), entries.join(""), "utf-8");
  }
  const summary = {
    format: "cowork-live-evidence/v1",
    mode: interactive ? "interactive" : "automated",
    ok: exitCode === 0,
    cleanup_succeeded: cleanupSucceeded,
    interactive_stop_reason: interactiveStopReason ?? null,
    failure: failure === undefined ? null : String(failure?.message ?? failure),
    fixture: fixture === undefined
      ? null
      : {
          format: fixture.format,
          initialized_store_id: fixture.initialized.store_id,
          source_sha256: fixture.source.sha256,
          sentinel_sha256: fixture.sentinel.sha256,
          scratch_snapshot_sha256: fixture.scratch.snapshot_sha256,
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
