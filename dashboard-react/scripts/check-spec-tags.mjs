/** Validate CI selection using Playwright's collected test metadata. */
import { existsSync, readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import ts from "typescript";

export const CI_TAG_PATTERN = "(^|\\s)@ci(?=\\s|$)";

const separateSpecs = Object.freeze({
  "live/cowork.spec.ts": {
    config: "playwright.live.config.ts",
    reason: "Requires the marker-guarded disposable backend, seeded Folders, and session authority.",
    tags: ["@live", "@no-ci"],
  },
});

export function tagDecision(tags) {
  const included = tags.includes("@ci");
  const excluded = tags.includes("@no-ci");
  if (included === excluded) return "Declare exactly one of @ci or @no-ci";
  return null;
}

function specFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const filename = path.join(directory, entry.name);
    if (entry.isDirectory()) return specFiles(filename);
    return /\.spec\.[cm]?[jt]sx?$/.test(entry.name) ? [filename] : [];
  });
}

export function separateSpecDecision(source, filename, requiredTags) {
  const parsed = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true);
  if (parsed.parseDiagnostics.length) return "Separately configured spec has invalid syntax";
  const importsTest = parsed.statements.some((statement) =>
    ts.isImportDeclaration(statement) &&
    statement.moduleSpecifier.text === "@playwright/test" &&
    statement.importClause?.namedBindings &&
    ts.isNamedImports(statement.importClause.namedBindings) &&
    statement.importClause.namedBindings.elements.some((binding) =>
      binding.name.text === "test" && (binding.propertyName?.text ?? "test") === "test"));
  if (!importsTest) return "Separately configured spec must import test from @playwright/test";
  const suites = parsed.statements.filter((statement) =>
    ts.isExpressionStatement(statement) && ts.isCallExpression(statement.expression) &&
    /^test\.describe(?:\.(?:serial|parallel))?$/.test(statement.expression.expression.getText(parsed)));
  if (suites.length !== 1) return "Separately configured spec must declare one top-level test.describe suite";
  const suite = suites[0].expression;
  const details = suite.arguments[1];
  const callback = suite.arguments[2];
  if (!details || !ts.isObjectLiteralExpression(details) || !callback ||
      !(ts.isArrowFunction(callback) || ts.isFunctionExpression(callback))) {
    return "Separately configured suite must declare literal tag details and a callback";
  }
  const tagProperty = details.properties.find((property) =>
    ts.isPropertyAssignment(property) && property.name.getText(parsed).replaceAll(/["']/g, "") === "tag");
  const initializer = tagProperty?.initializer;
  const tagNodes = initializer && ts.isArrayLiteralExpression(initializer)
    ? [...initializer.elements] : [initializer];
  if (tagNodes.some((node) => !node || !ts.isStringLiteral(node))) {
    return "Separately configured suite tags must be literal strings";
  }
  const tags = tagNodes.map((node) => node.text);
  if (tagDecision(tags) || requiredTags.some((tag) => !tags.includes(tag))) {
    return `Separately configured suite must declare ${requiredTags.join(" and ")}`;
  }
  let outsideRegistration = false;
  let conflictingDecision = false;
  let testCount = 0;
  const visit = (node) => {
    if (ts.isCallExpression(node) &&
        /^test(?:\.(?:only|skip|fixme|describe)(?:\.(?:serial|parallel|skip|only))?)?$/.test(node.expression.getText(parsed))) {
      if (node !== suite && (node.pos < callback.pos || node.end > callback.end)) {
        outsideRegistration = true;
      }
      if (/^test(?:\.(?:only|skip|fixme))?$/.test(node.expression.getText(parsed)) &&
          node.arguments[0] && ts.isStringLiteral(node.arguments[0])) testCount += 1;
    }
    if (ts.isStringLiteral(node) && /(^|\s)@ci(?=\s|$)/.test(node.text)) {
      conflictingDecision = true;
    }
    ts.forEachChild(node, visit);
  };
  visit(parsed);
  if (outsideRegistration) return "Every separately configured test must belong to the tagged suite";
  if (conflictingDecision) return "Separately configured specs cannot also opt into @ci";
  if (!testCount) return "Separately configured suite must contain tests";
  return null;
}

export function inspectSpecDecisions(rootDir, tests, declarations = separateSpecs) {
  const errors = [];
  const collectedFiles = new Set(tests.map((test) => path.resolve(test.location.file)));
  for (const test of tests) {
    const error = tagDecision(test.tags);
    if (error) errors.push(`${test.location.file}:${test.location.line}: ${error}`);
  }
  const files = specFiles(rootDir);
  const knownFiles = new Set(files.map((filename) => path.relative(rootDir, filename).split(path.sep).join("/")));
  for (const [relative, declaration] of Object.entries(declarations)) {
    if (!knownFiles.has(relative)) errors.push(`${relative}: separately configured spec does not exist`);
    if (!declaration.reason?.trim() || !declaration.config ||
        !existsSync(path.resolve(rootDir, "..", declaration.config))) {
      errors.push(`${relative}: declare an existing configuration and an exclusion reason`);
    }
  }
  for (const filename of files) {
    const relative = path.relative(rootDir, filename).split(path.sep).join("/");
    const declaration = declarations[relative];
    if (declaration) {
      const error = separateSpecDecision(readFileSync(filename, "utf-8"), filename, declaration.tags);
      if (error) errors.push(`${filename}: ${error}`);
    } else if (!collectedFiles.has(path.resolve(filename))) {
      errors.push(`${filename}: no collected test or explicit separate-configuration decision`);
    }
  }
  return { errors, collectedFiles: collectedFiles.size, separateFiles: Object.keys(declarations).length };
}

export default class SpecTagReporter {
  errors = [];

  onBegin(config, suite) {
    const result = inspectSpecDecisions(config.rootDir, suite.allTests());
    this.errors.push(...result.errors);
    console.log(`CI tag decisions checked for ${result.collectedFiles} collected and ${result.separateFiles} separately configured spec files.`);
  }

  onError(error) {
    this.errors.push(error.message ?? String(error));
  }

  onEnd(result) {
    for (const error of new Set(this.errors)) console.error(error);
    if (this.errors.length || result.status !== "passed") return { status: "failed" };
    return { status: "passed" };
  }
}
