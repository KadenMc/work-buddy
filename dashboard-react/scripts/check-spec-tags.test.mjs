import assert from "node:assert/strict";
import test from "node:test";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import SpecTagReporter, {
  CI_TAG_PATTERN,
  inspectSpecDecisions,
  separateSpecDecision,
  tagDecision,
} from "./check-spec-tags.mjs";

test("CI selection requires an explicit, unambiguous decision", () => {
  assert.equal(tagDecision(["@ci"]), null);
  assert.equal(tagDecision(["@no-ci", "@visual"]), null);
  assert.ok(tagDecision([]));
  assert.ok(tagDecision(["@ci", "@no-ci"]));
  assert.ok(tagDecision(["@ci-something"]));
});

test("a collection error fails the guard", () => {
  const reporter = new SpecTagReporter();
  reporter.onError(new Error("spec import failed"));
  assert.deepEqual(reporter.onEnd({ status: "failed" }), { status: "failed" });
});

test("CI selection matches the exact token without including prefix tags", () => {
  const selected = new RegExp(CI_TAG_PATTERN);
  assert.ok(selected.test("chromium journal.spec.ts user task @ci"));
  assert.ok(selected.test("@ci @keyboard"));
  assert.equal(selected.test("@no-ci @ci-extra"), false);
  assert.equal(selected.test("@circular"), false);
  assert.equal(selected.test("@no-ci"), false);
});

const separateSource = `
import { test } from '@playwright/test';
test.describe.serial('Co-work', { tag: ['@live', '@no-ci'] }, () => {
  test('opens the folder', async () => {});
});
`;

test("separately configured opt-outs require executable suite tags, not comments", () => {
  const inspect = (source) => separateSpecDecision(source, "cowork.spec.ts", ["@live", "@no-ci"]);
  assert.equal(inspect(separateSource), null);
  assert.ok(inspect(separateSource.replace("{ tag: ['@live', '@no-ci'] },", "/* @live @no-ci */")));
  assert.ok(inspect(separateSource.replace("'@no-ci'", "'@ci'")));
  assert.ok(inspect(separateSource + "test('outside', async () => {});"));
  assert.ok(inspect(separateSource.replace("test('opens the folder', async () => {});", "")));
  assert.ok(inspect(separateSource.replace("'opens the folder'", "'opens the folder @ci'")));
});

test("unknown spec directories and files cannot bypass collected CI decisions", () => {
  const tempRoot = mkdtempSync(path.join(os.tmpdir(), "work-buddy-ci-tags-"));
  const rootDir = path.join(tempRoot, "tests");
  const put = (relative, source = "") => {
    const target = path.join(rootDir, relative);
    mkdirSync(path.dirname(target), { recursive: true });
    writeFileSync(target, source);
    return target;
  };
  try {
    const ordinary = put("e2e/known.spec.ts");
    const tests = [{ location: { file: ordinary, line: 1 }, tags: ["@ci"] }];
    assert.deepEqual(inspectSpecDecisions(rootDir, tests, {}).errors, []);
    put("another-area/forgotten.spec.tsx", "// @ci");
    assert.match(inspectSpecDecisions(rootDir, tests, {}).errors.join("\n"), /forgotten\.spec\.tsx/);
    put("live/cowork.spec.ts", separateSource);
    const declarations = {
      "live/cowork.spec.ts": {
        config: "playwright.live.config.ts", reason: "Disposable backend required", tags: ["@live", "@no-ci"],
      },
    };
    writeFileSync(path.join(tempRoot, "playwright.live.config.ts"), "export default {};");
    const errors = inspectSpecDecisions(rootDir, tests, declarations).errors;
    assert.equal(errors.length, 1);
    assert.match(errors[0], /forgotten\.spec\.tsx/);
    put("live/unregistered.spec.ts", separateSource);
    assert.match(inspectSpecDecisions(rootDir, tests, declarations).errors.join("\n"), /unregistered\.spec\.ts/);
    declarations["live/absent.spec.ts"] = declarations["live/cowork.spec.ts"];
    assert.match(inspectSpecDecisions(rootDir, tests, declarations).errors.join("\n"), /absent\.spec\.ts:.*does not exist/);
  } finally {
    rmSync(tempRoot, { recursive: true, force: true });
  }
});
