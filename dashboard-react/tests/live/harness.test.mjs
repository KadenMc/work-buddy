import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import { createServer } from "node:http";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import { frontendLaunch, liveArtifactRoot, mintInteractiveUrl, parseOptions, removeHarnessRoot } from "./harness.mjs";

test("Playwright output cleanup cannot contain live session metadata or another runner's output", async () => {
  const dashboardRoot = fileURLToPath(new URL("../../", import.meta.url));
  const outputFor = async (filename) => {
    const source = ts.createSourceFile(filename, await readFile(path.join(dashboardRoot, filename), "utf-8"),
      ts.ScriptTarget.Latest, true);
    const exported = source.statements.find(ts.isExportAssignment);
    assert.ok(exported && ts.isCallExpression(exported.expression));
    const config = exported.expression.arguments[0];
    assert.ok(ts.isObjectLiteralExpression(config));
    const output = config.properties.find((property) =>
      ts.isPropertyAssignment(property) && property.name.getText(source) === "outputDir");
    if (output) assert.ok(ts.isStringLiteral(output.initializer));
    return path.resolve(dashboardRoot, output?.initializer.text ?? "test-results");
  };
  const ordinaryOutput = await outputFor("playwright.config.ts");
  const liveOutput = await outputFor("playwright.live.config.ts");
  const contains = (parent, child) => {
    const relative = path.relative(parent, child);
    return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
  };
  assert.ok(!contains(ordinaryOutput, liveArtifactRoot), "ordinary cleanup must not delete live metadata");
  assert.ok(!contains(ordinaryOutput, liveOutput), "ordinary cleanup must not delete live test results");
  assert.ok(!contains(liveOutput, ordinaryOutput), "live cleanup must not delete ordinary test results");
  assert.ok(!contains(liveOutput, liveArtifactRoot), "live test cleanup must preserve harness metadata");
});

test("interactive development reloads source; an explicit build serves the bundle", () => {
  assert.equal(parseOptions([]).dev, false);
  assert.equal(parseOptions([]).scenario, "lifecycle");
  assert.equal(parseOptions(["--app", "tasks"]).scenario, "browse");
  assert.equal(parseOptions(["--app", "tasks", "--scenario", "organization"]).seeder, "seeds/tasks.py");
  assert.throws(() => parseOptions(["--app", "tasks", "--scenario", "truth-panel"]), /Unknown scenario/);
  assert.equal(parseOptions(["--scenario", "truth-panel"]).scenario, "truth-panel");
  assert.throws(() => parseOptions(["--scenario", "missing"]), /Unknown scenario/);
  assert.equal(parseOptions(["--interactive"]).dev, true);
  assert.equal(parseOptions(["--dev"]).interactive, true);
  const built = parseOptions(["--interactive", "--dev", "--build", "--app", "cowork"]);
  assert.equal(built.dev, false);
  assert.equal(built.interactive, true);
  assert.match(built.mode, /source edits require a restart/);
  assert.throws(() => parseOptions(["--app", "missing"]), /Unknown app/);
  for (const port of ["5127", "0", "80", "65536", "no-port"]) {
    assert.throws(() => parseOptions(["--frontend-port", port]), /unprivileged port/);
  }
});

test("both frontend modes replace an inherited production proxy with the isolated backend", () => {
  for (const args of [["--dev"], ["--build"]]) {
    const options = parseOptions(args);
    const result = frontendLaunch("vite.js", options, "http://127.0.0.1:54321", 5188, {
      WB_DASHBOARD_PROXY_TARGET: "http://127.0.0.1:5127",
    });
    assert.equal(result.env.WB_DASHBOARD_PROXY_TARGET, "http://127.0.0.1:54321");
    assert.equal(result.args.includes("preview"), !options.dev);
    assert.ok(result.args.includes("--strictPort"));
    assert.throws(() => frontendLaunch("vite.js", options, "http://127.0.0.1:5127", 5188, {}));
  }
});

test("interactive URL mint carries the exact frontend origin and nonce into a fragment", async () => {
  const requests = [];
  const server = createServer(async (request, response) => {
    let body = "";
    for await (const chunk of request) body += chunk;
    requests.push({ method: request.method, url: request.url, headers: request.headers, body });
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ ok: true, token: "wbb_isolated_token" }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const origin = `http://127.0.0.1:${server.address().port}`;
    const url = new URL(await mintInteractiveUrl(origin, "cowork", "fixture-nonce"));
    assert.equal(url.origin, origin);
    assert.equal(url.pathname, "/app/cowork");
    assert.equal(url.hash, "#wb-bootstrap=wbb_isolated_token");
    assert.equal(requests.length, 1);
    assert.equal(requests[0].method, "POST");
    assert.equal(requests[0].url, "/api/_live/identity-bootstrap");
    assert.equal(requests[0].headers.origin, origin);
    assert.equal(requests[0].headers["x-wb-live-control"], "fixture-nonce");
    assert.deepEqual(JSON.parse(requests[0].body), { origin });
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test("cleanup refuses an unmarked or unexpected root and deletes only a marked harness root", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "work-buddy-live-"));
  const unexpected = await mkdtemp(path.join(os.tmpdir(), "work-buddy-unrelated-"));
  try {
    const sentinel = path.join(root, "keep.txt");
    await writeFile(sentinel, "retain without marker");
    await assert.rejects(removeHarnessRoot(root));
    assert.equal(await readFile(sentinel, "utf-8"), "retain without marker");
    await writeFile(path.join(unexpected, ".wb-live-harness"), "marker");
    await assert.rejects(removeHarnessRoot(unexpected), /unexpected path/);
    await mkdir(path.join(root, ".wb-live-harness"));
    await assert.rejects(removeHarnessRoot(root), /must be a file/);
    await rm(path.join(root, ".wb-live-harness"), { recursive: true });
    await writeFile(path.join(root, ".wb-live-harness"), "marker");
    await removeHarnessRoot(root);
    await assert.rejects(stat(root), { code: "ENOENT" });
    assert.ok((await stat(unexpected)).isDirectory());
  } finally {
    await rm(root, { recursive: true, force: true });
    await rm(unexpected, { recursive: true, force: true });
  }
});
