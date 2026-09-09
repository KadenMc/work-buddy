import { readFile } from "node:fs/promises";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const fixturePath = process.env.WB_LIVE_FIXTURE_FILE;
const baseURL = process.env.WB_LIVE_BASE_URL;
const nonce = process.env.WB_LIVE_HARNESS_NONCE;
if (!fixturePath || !baseURL || !nonce) throw new Error("Use the isolated Tasks live harness.");
const fixture = JSON.parse(await readFile(fixturePath, "utf8"));
if (fixture.app !== "tasks" || fixture.harness.nonce !== nonce || new URL(baseURL).hostname !== "127.0.0.1" || new URL(baseURL).port === "5127") throw new Error("Refusing a non-Tasks or nonisolated browser fixture.");
await readFile(path.join(fixture.root, ".wb-live-harness"));

const workspace = (page: Page) => page.getByRole("region", { name: "Task workspace", exact: true });
const rows = (page: Page) => workspace(page).locator(".wb-task-list__select");
const count = (page: Page) => workspace(page).locator(".wb-task-results-bar [role=status]");

async function openTasks(page: Page) {
  await page.goto(`${baseURL}/app/tasks`);
  const token = await page.evaluate(async (control) => {
    const response = await fetch("/api/_live/identity-bootstrap", {
      method: "POST", headers: { "Content-Type": "application/json", "X-WB-Live-Control": control },
      body: JSON.stringify({ origin: location.origin }),
    });
    if (!response.ok) throw new Error("Isolated identity bootstrap failed.");
    return (await response.json()).token as string;
  }, nonce!);
  await page.goto(`${baseURL}/app/tasks#wb-bootstrap=${encodeURIComponent(token)}`);
  await expect.poll(async () => page.evaluate(async () => {
    const session = await (await fetch("/api/local-identity/session")).json();
    return session.authenticated === true && session.principal?.origin === location.origin;
  })).toBe(true);
  await expect(count(page)).toHaveText("139 tasks");
}

async function taskState(page: Page) {
  const response = await page.request.get(`${baseURL}/api/tasks/view?statuses=&limit=200`);
  expect(response.ok()).toBe(true);
  return (await response.json()).tasks as { task_id: string; revision: number; namespaces: string[]; project_ids: number[]; unresolved_projects: unknown[]; status: string }[];
}

test.describe.serial("Task Workspace live journeys", { tag: ["@live", "@no-ci"] }, () => {
  test.beforeEach(async ({ page }) => { await openTasks(page); });

  test("defaults, immediate multiselect filters and global sort @firefox-smoke", async ({ page }) => {
    await expect(rows(page).first()).toContainText("Recently captured task");
    await expect(rows(page).first()).toContainText("Created");
    const secondRow = await rows(page).nth(1).boundingBox();
    expect(secondRow!.y + secondRow!.height).toBeLessThanOrEqual(page.viewportSize()!.height);
    await page.screenshot({ path: test.info().outputPath("desktop-browse.png") });
    await expect(workspace(page).getByRole("tab")).toHaveCount(0);
    await page.getByRole("button", { name: "Status, 1 selected", exact: true }).click();
    await page.getByRole("checkbox", { name: "Completed", exact: true }).check();
    // A modal picker keeps focus inside while applying each selection.
    await expect.poll(async () => new URL(page.url()).searchParams.getAll("statuses")).toContain("completed");
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await expect(count(page)).toHaveText("140 tasks");
    await expect(page.getByRole("button", { name: "Remove Completed filter", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Remove Completed filter", exact: true }).click();
    await expect(count(page)).toHaveText("139 tasks");
    await page.getByRole("combobox", { name: "Sort by", exact: true }).selectOption("title");
    await expect(page.getByRole("button", { name: "Reverse sort direction, currently A–Z", exact: true })).toBeVisible();
    await expect(rows(page).first()).toContainText("Check the existing destination namespace");
    await page.getByRole("button", { name: "Reverse sort direction, currently A–Z", exact: true }).click();
    await expect(rows(page).first()).toContainText("Write the bounded results section");
    await page.getByRole("combobox", { name: "Sort by", exact: true }).selectOption("created_at");
    await expect(rows(page).first()).toContainText("Recently captured task");
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await expect(page.getByRole("navigation", { name: "Task pages" })).toContainText("51–100 of 139");
    await expect(rows(page)).toHaveCount(50);
  });

  test("namespace selection survives reclaimed space and No namespace count is accurate", async ({ page }) => {
    const rail = page.getByRole("complementary", { name: "Namespaces", exact: true });
    await expect(rail).toBeVisible();
    await rail.getByRole("checkbox", { name: "projects and descendants", exact: true }).check();
    await expect(count(page)).toHaveText("4 tasks");
    const before = await workspace(page).locator(".wb-task-results").boundingBox();
    await page.getByRole("button", { name: "Hide namespaces", exact: true }).click();
    await expect(rail).toHaveCount(0);
    const after = await workspace(page).locator(".wb-task-results").boundingBox();
    expect(after!.width - before!.width).toBeGreaterThan(180);
    await expect(page.getByRole("button", { name: "Remove projects + descendants filter", exact: true })).toBeVisible();
    await expect(count(page)).toHaveText("4 tasks");
    await page.getByRole("button", { name: "Remove projects + descendants filter", exact: true }).click();
    await page.getByRole("button", { name: "Show namespaces", exact: true }).click();
    await rail.getByRole("checkbox", { name: "No namespace", exact: true }).check();
    await expect(count(page)).toHaveText("3 tasks");
    await expect(rows(page)).toHaveCount(3);
    await expect(rows(page).filter({ hasText: "Project association without namespace" })).toHaveCount(1);
  });

  test("projects stay independent and dedicated details return to the same query and focus", async ({ page }) => {
    await page.getByRole("button", { name: "Projects, any", exact: true }).click();
    await page.getByRole("checkbox", { name: "ECG Research", exact: true }).check();
    await page.keyboard.press("Escape");
    await expect(count(page)).toHaveText("5 tasks");
    await page.getByRole("combobox", { name: "Sort by", exact: true }).selectOption("title");
    const task = rows(page).filter({ hasText: "Improve task browsing" });
    await expect(task).toContainText("Work Buddy");
    await expect(task).toContainText("ECG Research");
    await task.click();
    await expect(page.getByRole("textbox", { name: "Title", exact: true })).toHaveValue("Improve task browsing");
    await expect(rows(page)).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Linked projects, 2 selected", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Back to tasks", exact: true }).click();
    await expect(count(page)).toHaveText("5 tasks");
    await expect(task).toBeFocused();
    await expect(page.getByRole("combobox", { name: "Sort by", exact: true })).toHaveValue("title");
    expect(new URL(page.url()).searchParams.get("projects")).toBe(String(fixture.tasks.project_ids.ecg));
  });

  test("completion help, confirmation, cancellation and recovery work in browse and detail", async ({ page }) => {
    const title = "Recently captured task";
    const complete = page.getByRole("button", { name: `Complete ${title}`, exact: true });
    let completions = 0;
    page.on("request", (request) => {
      if (request.method() === "POST" && new URL(request.url()).pathname.endsWith("/complete")) completions++;
    });
    await expect(complete).toHaveAttribute("title", "Open a confirmation before completing this task.");
    await page.getByRole("button", { name: "Hover help", exact: true }).click();
    await complete.hover();
    await expect(page.getByRole("tooltip")).toContainText("The checkmark does not change anything until you confirm.");
    await expect(page.getByRole("tooltip")).toContainText("The separate square checkbox only selects the task.");
    await page.screenshot({ path: test.info().outputPath("completion-help.png") });
    expect(completions).toBe(0);
    await page.getByRole("button", { name: "Hover help", exact: true }).click();
    const dialog = page.getByRole("alertdialog", { name: "Complete this task?", exact: true });
    await complete.click();
    await expect(dialog).toContainText(title);
    await expect(dialog.getByRole("button", { name: "Cancel", exact: true })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(dialog).toHaveCount(0);
    await expect(complete).toBeFocused();
    expect(completions).toBe(0);
    await complete.click();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: test.info().outputPath("completion-confirmation.png") });
    const box = await dialog.boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(390);
    await dialog.getByRole("button", { name: "Mark complete", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await expect(count(page)).toHaveText("138 tasks");
    expect(completions).toBe(1);
    await page.getByRole("button", { name: "Status, 1 selected", exact: true }).click();
    await page.getByRole("checkbox", { name: "Completed", exact: true }).check();
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await rows(page).filter({ hasText: title }).click();
    await page.getByRole("button", { name: "Reopen", exact: true }).click();
    await expect(page.getByRole("button", { name: "Complete", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Complete", exact: true }).click();
    await expect(dialog).toContainText(title);
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Complete", exact: true })).toBeFocused();
    expect(completions).toBe(1);
    await page.getByRole("button", { name: "Back to tasks", exact: true }).click();
    await page.getByRole("button", { name: "Remove Completed filter", exact: true }).click();
    await expect(count(page)).toHaveText("139 tasks");
  });

  test("review, cancel, apply and durable Undo preserve task and project identity across all statuses", async ({ page }) => {
    const before = await taskState(page);
    await page.getByRole("button", { name: "Hide namespaces", exact: true }).click();
    await page.getByRole("button", { name: "Manage namespaces", exact: true }).click();
    const organizer = page.getByRole("region", { name: "Namespace organizer", exact: true });
    await expect(organizer.getByRole("heading", { name: "Manage namespaces", exact: true })).toBeVisible();
    await expect(organizer.getByRole("button", { name: "Preview change", exact: true })).toHaveCount(0);
    const branch = organizer.getByRole("checkbox", { name: "projects", exact: true });
    await expect(branch.locator("..")).toContainText("1 direct · 7 including descendants");
    await page.screenshot({ path: test.info().outputPath("namespace-inventory.png") });
    await branch.check();
    const configure = async () => {
      await organizer.getByRole("button", { name: "Move children up one level", exact: true }).click();
      await organizer.getByRole("radio", { name: "Keep assignments on this namespace", exact: true }).check();
      await organizer.getByRole("button", { name: "Preview change", exact: true }).click();
      await expect(organizer).toContainText("1 archived · 1 completed · 3 open · 1 trash");
      await expect(organizer.getByRole("button", { name: "Move children up one level · 6 tasks", exact: true })).toBeDisabled();
      await organizer.getByRole("checkbox", { name: /^Merge assignments into the existing destinations/ }).check();
      await expect(organizer.getByRole("button", { name: "Move children up one level · 6 tasks", exact: true })).toBeEnabled();
    };
    await configure();
    await page.screenshot({ path: test.info().outputPath("namespace-preview.png") });
    expect(new URL(page.url()).hash).not.toContain("wb-bootstrap");
    await organizer.getByRole("button", { name: "Cancel change", exact: true }).click();
    expect(await taskState(page)).toEqual(before);
    await configure();
    await organizer.getByRole("button", { name: "Inspect affected tasks", exact: true }).click();
    await expect(organizer).toContainText("Trashed namespace membership");
    let refreshes = 0;
    page.on("request", (request) => { if (new URL(request.url()).pathname === "/api/tasks/view") refreshes++; });
    await organizer.getByRole("button", { name: "Move children up one level · 6 tasks", exact: true }).click();
    await expect(organizer).toContainText("Project links and task identities were preserved.");
    expect(refreshes).toBeLessThan(8);
    const after = await taskState(page);
    const identity = (tasks: typeof after) => tasks.map(({ task_id, project_ids, unresolved_projects, status }) => ({ task_id, project_ids, unresolved_projects, status })).sort((a, b) => a.task_id.localeCompare(b.task_id));
    expect(identity(after)).toEqual(identity(before));
    expect(after.find((task) => task.task_id === fixture.tasks.task_ids.shared)!.namespaces).toEqual(["research/analysis", "work-buddy/ui"]);
    await page.reload();
    await organizer.getByText("Recent namespace changes", { exact: true }).click();
    await organizer.getByRole("button", { name: "Undo promote namespaces on 6 tasks", exact: true }).click();
    await expect(organizer).toContainText("Namespace change undone.");
    const restored = await taskState(page);
    expect(identity(restored)).toEqual(identity(before));
    expect(restored.map(({ task_id, namespaces }) => ({ task_id, namespaces })).sort((a, b) => a.task_id.localeCompare(b.task_id))).toEqual(before.map(({ task_id, namespaces }) => ({ task_id, namespaces })).sort((a, b) => a.task_id.localeCompare(b.task_id)));
  });

  test("narrow project picker stays usable and task edits survive Back and reload", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const secondRow = await rows(page).nth(1).boundingBox();
    expect(secondRow!.y + secondRow!.height).toBeLessThanOrEqual(844);
    await page.screenshot({ path: test.info().outputPath("narrow-browse.png") });
    const original = "Improve task browsing";
    await page.getByRole("searchbox", { name: "Search tasks", exact: true }).fill(original);
    await expect(rows(page)).toHaveCount(1);
    await rows(page).first().click();
    const title = page.getByRole("textbox", { name: "Title", exact: true });
    await expect(title).toHaveValue(original);
    const projects = page.getByRole("button", { name: "Linked projects, 2 selected", exact: true });
    await projects.click();
    const picker = page.getByRole("dialog", { name: "Linked projects options", exact: true });
    const ecg = picker.getByRole("checkbox", { name: "ECG Research", exact: true });
    await ecg.uncheck();
    await ecg.check();
    const box = await picker.boundingBox();
    expect(box!.y).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(390);
    expect(box!.y + box!.height).toBeLessThanOrEqual(844);
    await page.screenshot({ path: test.info().outputPath("narrow-project-picker.png") });
    await picker.getByRole("button", { name: "Done", exact: true }).click();
    await expect(projects).toBeFocused();
    await title.fill("Retained workspace edit");
    await page.getByRole("button", { name: "Back to tasks", exact: true }).click();
    await expect(rows(page)).toHaveCount(1);
    await rows(page).first().click();
    await expect(title).toHaveValue("Retained workspace edit");
    await page.reload();
    await expect(title).toHaveValue("Retained workspace edit");
    await page.getByRole("button", { name: "Save changes", exact: true }).click();
    await expect(page.getByText("All changes saved", { exact: true })).toBeVisible();
    await page.reload();
    await expect(title).toHaveValue("Retained workspace edit");
    await expect(projects).toBeVisible();
    await expect(page.getByRole("textbox", { name: "Namespaces", exact: true })).toHaveValue("projects/work-buddy/ui, research/analysis");
    await title.fill(original);
    await page.getByRole("button", { name: "Save changes", exact: true }).click();
    await expect(page.getByText("All changes saved", { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
});
