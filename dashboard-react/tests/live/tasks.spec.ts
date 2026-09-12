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
const rows = (page: Page) => workspace(page).locator(".wb-task-list > li");
const titleButtons = (page: Page) => workspace(page).locator(".wb-task-list__select");
const count = (page: Page) => workspace(page).locator(".wb-task-results-bar [role=status]");
const namespaceRail = (page: Page) => page.getByRole("complementary", { name: "Namespaces", exact: true });
const sortField = (page: Page) => workspace(page).getByRole("group", { name: "Task sorting", exact: true }).getByRole("button", { name: /Sort by/ });
const personalizationKey = "work-buddy.dashboard.personalization.v1:wb.tasks.workspace";

async function chooseSort(page: Page, label: string) {
  await sortField(page).click();
  await page.getByRole("option", { name: label, exact: true }).click();
  await expect(sortField(page)).toContainText(label);
}

async function expectNamespaceCountGutter(page: Page) {
  const tree = namespaceRail(page).getByRole("list", { name: "Namespace hierarchy", exact: true });
  await expect(tree).toBeVisible();
  const gaps = await tree.evaluate((element) => {
    // clientWidth stops before the scrollbar (including its reserved gutter).
    // Measure the usable edge rather than the scroller's outer border.
    const usableRight = element.getBoundingClientRect().left + element.clientLeft + element.clientWidth;
    return Array.from(element.querySelectorAll(".wb-task-namespace-tree__row small"), (count) => usableRight - count.getBoundingClientRect().right);
  });
  expect(gaps.length).toBeGreaterThan(0);
  for (const gap of gaps) {
    expect(gap, "Namespace count has a 12px scrollbar-side gutter").toBeGreaterThanOrEqual(11);
    expect(gap, "Namespace counts share the same inset").toBeLessThanOrEqual(14);
  }
}

async function bringWorkspaceIntoView(page: Page) {
  const heading = page.getByRole("region", { name: "Task Workspace", exact: true }).getByRole("heading", { name: "Task Workspace", exact: true });
  await heading.evaluate((element) => element.scrollIntoView({ block: "start" }));
}

async function savedLayout(page: Page) {
  return page.evaluate((key) => {
    const raw = localStorage.getItem(key);
    return raw === null ? null : JSON.parse(raw) as { defaultSlotOverrides: Record<string, { layout?: { x: number; y: number; w: number; h: number } }> };
  }, personalizationKey);
}

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
    await expectNamespaceCountGutter(page);
    const capture = page.getByRole("region", { name: "Quick Add", exact: true });
    const frameBox = (await capture.boundingBox())!;
    const contentBox = (await capture.locator(".wb-widget-frame__content").boundingBox())!;
    for (const label of ["Add details", "AI help", "Save proposal"]) {
      const action = capture.getByRole("button", { name: label, exact: true });
      await expect(action).toBeVisible();
      const box = (await action.boundingBox())!;
      expect(box.y, `${label} starts inside Quick Add`).toBeGreaterThanOrEqual(contentBox.y - 1);
      expect(box.y + box.height, `${label} is not clipped by Quick Add`).toBeLessThanOrEqual(Math.min(frameBox.y + frameBox.height, contentBox.y + contentBox.height) + 1);
      expect(box.x).toBeGreaterThanOrEqual(contentBox.x - 1);
      expect(box.x + box.width).toBeLessThanOrEqual(contentBox.x + contentBox.width + 1);
    }
    await page.screenshot({ path: test.info().outputPath("desktop-page-default.png") });
    // The page preserves the user's grid positions and capture widget. Assess
    // browsing density after bringing the Task Workspace into view.
    await bringWorkspaceIntoView(page);
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
    await expect(sortField(page)).toHaveClass(/wb-select-field__trigger/);
    await chooseSort(page, "Title");
    await expect(page.getByRole("button", { name: "Reverse sort direction, currently A–Z", exact: true })).toBeVisible();
    await expect(rows(page).first()).toContainText("Check the existing destination namespace");
    await page.getByRole("button", { name: "Reverse sort direction, currently A–Z", exact: true }).click();
    await expect(rows(page).first()).toContainText("Write the bounded results section");
    await chooseSort(page, "Date created");
    await expect(rows(page).first()).toContainText("Recently captured task");
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await expect(page.getByRole("navigation", { name: "Task pages" })).toContainText("51–100 of 139");
    await expect(rows(page)).toHaveCount(50);
  });

  test("Customize view restores Arrange, safe Preview and a saved resize after reload", async ({ page }) => {
    const beforeTasks = await taskState(page);
    const gridItem = page.locator('.wb-dashboard-grid-item[data-widget-instance-id="wb-tasks:workspace"]');
    const controls = page.getByRole("toolbar", { name: "View controls", exact: true });
    const customize = page.getByRole("button", { name: "Customize view", exact: true });
    const handle = gridItem.getByRole("button", { name: /Move or resize widget/ });
    const before = await gridItem.boundingBox();
    expect(before).not.toBeNull();
    expect(await savedLayout(page)).toBeNull();
    await expect(customize).toBeEnabled();
    await customize.click();
    await expect(controls).toContainText("Arranging layout");
    await expect(gridItem.locator(".wb-widget-frame__content")).toHaveAttribute("inert", "");
    await expect(gridItem.locator(".wb-widget-resize-handle")).toHaveCount(8);
    await handle.press("Shift+ArrowDown");
    await expect(controls.getByRole("button", { name: "Done", exact: true })).toBeEnabled();
    await controls.getByRole("button", { name: "Preview interactions", exact: true }).click();
    await expect(controls).toContainText("Previewing interactions");
    await expect(gridItem.locator(".wb-widget-frame__content")).not.toHaveAttribute("inert", "");
    await expect(gridItem.locator(".wb-widget-resize-handle")).toHaveCount(0);
    await page.getByRole("button", { name: "Status, 1 selected", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "Status options", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await controls.getByRole("button", { name: "Back to arranging", exact: true }).click();
    await controls.getByRole("button", { name: "Cancel", exact: true }).click();
    expect(await savedLayout(page)).toBeNull();
    await expect.poll(async () => (await gridItem.boundingBox())!.height).toBeCloseTo(before!.height, 0);

    await customize.click();
    await handle.press("Shift+ArrowDown");
    await controls.getByRole("button", { name: "Done", exact: true }).click();
    await expect(customize).toBeEnabled();
    await expect.poll(async () => (await savedLayout(page))?.defaultSlotOverrides.workspace?.layout?.h).toBeGreaterThan(0);
    const saved = await savedLayout(page);
    const resized = await gridItem.boundingBox();
    expect(resized!.height - before!.height).toBeGreaterThan(20);
    await page.screenshot({ path: test.info().outputPath("customized-task-layout.png") });
    await page.reload();
    await expect(count(page)).toHaveText("139 tasks");
    expect(await savedLayout(page)).toEqual(saved);
    await expect.poll(async () => (await gridItem.boundingBox())!.height).toBeCloseTo(resized!.height, 0);
    expect(await taskState(page)).toEqual(beforeTasks);
  });

  test("namespace hierarchy starts collapsed and its shared divider retains a deliberate width", async ({ page }) => {
    const rail = namespaceRail(page);
    await expect(rail.getByRole("button", { name: "Expand projects", exact: true })).toHaveAttribute("aria-expanded", "false");
    await expect(rail.getByRole("checkbox", { name: "projects/work-buddy and descendants", exact: true })).toHaveCount(0);
    await rail.getByRole("button", { name: "Expand projects", exact: true }).click();
    await expect(rail.getByRole("button", { name: "Collapse projects", exact: true })).toHaveAttribute("aria-expanded", "true");
    await expect(rail.getByRole("checkbox", { name: "projects/work-buddy and descendants", exact: true })).toBeVisible();
    await page.reload();
    await expect(count(page)).toHaveText("139 tasks");
    await expect(rail.getByRole("button", { name: "Collapse projects", exact: true })).toHaveAttribute("aria-expanded", "true");
    await expect(rail.getByRole("checkbox", { name: "projects/work-buddy and descendants", exact: true })).toBeVisible();
    await rail.getByRole("button", { name: "Collapse projects", exact: true }).click();
    await expect(rail.getByRole("checkbox", { name: "projects/work-buddy and descendants", exact: true })).toHaveCount(0);

    const divider = workspace(page).getByRole("separator", { name: "Resize namespaces", exact: true });
    await expect(divider).toHaveClass(/wb-workspace-side-panel__separator/);
    const initialWidth = (await rail.boundingBox())!.width;
    expect(initialWidth).toBeGreaterThanOrEqual(200);
    expect(initialWidth).toBeLessThanOrEqual(310);
    await bringWorkspaceIntoView(page);
    await page.screenshot({ path: test.info().outputPath("namespace-collapsed-default-width.png") });
    await rail.getByRole("searchbox", { name: "Find namespaces", exact: true }).fill("/");
    const tree = rail.getByRole("list", { name: "Namespace hierarchy", exact: true });
    await expect.poll(() => tree.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);
    await expectNamespaceCountGutter(page);
    await page.screenshot({ path: test.info().outputPath("namespace-count-scrollbar-gutter.png") });
    await rail.getByRole("searchbox", { name: "Find namespaces", exact: true }).clear();
    await divider.scrollIntoViewIfNeeded();
    const dividerBox = (await divider.boundingBox())!;
    const pointerY = Math.max(10, Math.min(dividerBox.y + 24, page.viewportSize()!.height - 24));
    await page.mouse.move(dividerBox.x + dividerBox.width / 2, pointerY);
    await page.mouse.down();
    await page.mouse.move(dividerBox.x + dividerBox.width / 2 + 60, pointerY, { steps: 8 });
    await page.mouse.up();
    await expect.poll(async () => (await rail.boundingBox())!.width).toBeGreaterThan(initialWidth + 35);
    const draggedWidth = (await rail.boundingBox())!.width;
    await divider.focus();
    await divider.press("ArrowLeft");
    await expect.poll(async () => (await rail.boundingBox())!.width).toBeLessThan(draggedWidth - 1);
    const resizedWidth = (await rail.boundingBox())!.width;
    await page.reload();
    await expect(count(page)).toHaveText("139 tasks");
    await expect.poll(async () => Math.abs((await rail.boundingBox())!.width - resizedWidth)).toBeLessThan(3);
    await expect(rail.getByRole("button", { name: "Expand projects", exact: true })).toBeVisible();
    await page.screenshot({ path: test.info().outputPath("resized-namespace-pane.png") });
    await divider.dblclick();
    await expect.poll(async () => Math.abs((await rail.boundingBox())!.width - initialWidth)).toBeLessThan(3);
    await divider.scrollIntoViewIfNeeded();
    const minimumBox = (await divider.boundingBox())!;
    const minimumY = Math.max(10, Math.min(minimumBox.y + 24, page.viewportSize()!.height - 24));
    await page.mouse.move(minimumBox.x + minimumBox.width / 2, minimumY);
    await page.mouse.down();
    await page.mouse.move(0, minimumY, { steps: 8 });
    await page.mouse.up();
    await expect.poll(async () => (await rail.boundingBox())!.width).toBeLessThanOrEqual(165);
    expect((await rail.boundingBox())!.width).toBeGreaterThanOrEqual(159);
    await rail.getByRole("searchbox", { name: "Find namespaces", exact: true }).fill("projects/ecg/deep/analysis");
    const deep = rail.getByRole("checkbox", { name: "projects/ecg/deep/analysis and descendants", exact: true });
    await expect(deep).toBeVisible();
    const deepName = deep.locator("xpath=ancestor::label").locator(":scope > span").last();
    expect((await deepName.boundingBox())!.width).toBeGreaterThanOrEqual(30);
    await expectNamespaceCountGutter(page);
    await page.screenshot({ path: test.info().outputPath("namespace-minimum-width.png") });
    await rail.getByRole("searchbox", { name: "Find namespaces", exact: true }).clear();
    await divider.dblclick();
    await expect.poll(async () => Math.abs((await rail.boundingBox())!.width - initialWidth)).toBeLessThan(3);
  });

  test("namespace selection survives reclaimed space and No namespace count is accurate", async ({ page }) => {
    const rail = namespaceRail(page);
    await expect(rail).toBeVisible();
    await expect(page.getByRole("button", { name: "Manage namespaces", exact: true })).toHaveCount(1);
    await expect(rail.getByRole("button", { name: "Manage namespaces", exact: true })).toBeVisible();
    await rail.getByRole("checkbox", { name: "projects and descendants", exact: true }).check();
    await expect(count(page)).toHaveText("4 tasks");
    const before = await workspace(page).locator(".wb-task-results").boundingBox();
    await page.getByRole("button", { name: "Hide namespaces", exact: true }).click();
    await expect(rail).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Manage namespaces", exact: true })).toHaveCount(0);
    const showNamespaces = page.getByRole("button", { name: "Show namespaces", exact: true });
    await expect(showNamespaces).toBeVisible();
    const showBox = (await showNamespaces.boundingBox())!;
    const sortBox = (await workspace(page).getByRole("group", { name: "Task sorting", exact: true }).boundingBox())!;
    expect(showBox.x + showBox.width).toBeLessThanOrEqual(sortBox.x);
    const after = await workspace(page).locator(".wb-task-results").boundingBox();
    expect(after!.width - before!.width).toBeGreaterThan(180);
    expect(showBox.x - after!.x).toBeLessThan(12);
    await expect(page.getByRole("button", { name: "Remove projects + descendants filter", exact: true })).toBeVisible();
    await expect(count(page)).toHaveText("4 tasks");
    await page.getByRole("button", { name: "Remove projects + descendants filter", exact: true }).click();
    await page.getByRole("button", { name: "Show namespaces", exact: true }).click();
    await rail.getByRole("checkbox", { name: "No namespace", exact: true }).check();
    await expect(count(page)).toHaveText("3 tasks");
    await expect(rows(page)).toHaveCount(3);
    await expect(rows(page).filter({ hasText: "Project association without namespace" })).toHaveCount(1);
  });

  test("namespace checkbox cycles through branch and exact scope, and its eraser preserves other filters", async ({ page }) => {
    const before = await taskState(page);
    const rail = namespaceRail(page);
    await page.getByRole("button", { name: "Status, 1 selected", exact: true }).click();
    await page.getByRole("checkbox", { name: "Completed", exact: true }).check();
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await chooseSort(page, "Title");
    await page.getByRole("button", { name: "Reverse sort direction, currently A–Z", exact: true }).click();
    const branch = () => rail.getByRole("checkbox", { name: "projects and descendants", exact: true });
    const exact = () => rail.getByRole("checkbox", { name: "projects only", exact: true });
    const eraser = rail.getByRole("button", { name: "Clear namespace filters", exact: true });
    await expect(eraser).toBeDisabled();
    await expect(branch()).toHaveAttribute("aria-checked", "false");
    await branch().check();
    await expect(branch()).toHaveAttribute("aria-checked", "true");
    await expect(rail.locator('.wb-task-namespace-check[data-scope="branch"]')).toHaveCount(1);
    await expect(count(page)).toHaveText("5 tasks");
    await bringWorkspaceIntoView(page);
    await page.screenshot({ path: test.info().outputPath("namespace-branch-checkbox.png") });
    await branch().press("Space");
    await expect(exact()).toHaveAttribute("aria-checked", "mixed");
    await expect(exact()).toHaveJSProperty("indeterminate", true);
    await expect(count(page)).toHaveText("1 task");
    await expect(rows(page).first()).toContainText("Resolve tasks directly in the grouping namespace");
    expect(new URL(page.url()).searchParams.getAll("exact_namespaces")).toEqual(["projects"]);
    await page.screenshot({ path: test.info().outputPath("namespace-exact-checkbox.png") });
    await exact().press("Space");
    await expect(branch()).toHaveAttribute("aria-checked", "false");
    await expect(count(page)).toHaveText("140 tasks");
    await branch().check();
    await branch().click();
    await expect(exact()).toHaveAttribute("aria-checked", "mixed");
    await rail.getByRole("checkbox", { name: "No namespace", exact: true }).check();
    await eraser.click();
    await expect(count(page)).toHaveText("140 tasks");
    await expect(eraser).toBeDisabled();
    await expect(branch()).toHaveAttribute("aria-checked", "false");
    await expect(rail.getByRole("checkbox", { name: "No namespace", exact: true })).not.toBeChecked();
    const params = new URL(page.url()).searchParams;
    expect(params.getAll("namespaces").filter(Boolean)).toEqual([]);
    expect(params.getAll("exact_namespaces").filter(Boolean)).toEqual([]);
    expect(params.getAll("statuses")).toEqual(["open", "completed"]);
    expect(params.get("sort")).toBe("title");
    expect(params.get("direction")).toBe("desc");
    expect(await taskState(page)).toEqual(before);
  });

  test("namespace choices follow other filters while selected empty branches remain removable", async ({ page }) => {
    const rail = namespaceRail(page);
    const trashPath = "projects/work-buddy/trash";
    await rail.getByRole("button", { name: "Expand projects", exact: true }).click();
    await rail.getByRole("button", { name: "Expand projects/work-buddy", exact: true }).click();
    await expect(rail.getByRole("checkbox", { name: `${trashPath} and descendants`, exact: true })).toHaveCount(0);
    await expect(rail.getByRole("checkbox", { name: "projects/work-buddy/archive and descendants", exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Status, 1 selected", exact: true }).click();
    await page.getByRole("checkbox", { name: "Trash", exact: true }).check();
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await expect(count(page)).toHaveText("140 tasks");
    const trashNamespace = rail.getByRole("checkbox", { name: `${trashPath} and descendants`, exact: true });
    await expect(trashNamespace).toBeVisible();
    await trashNamespace.check();
    await expect(count(page)).toHaveText("1 task");
    // Namespace choices use the other filters, so choosing one branch does not
    // erase a sibling that could be added to the same OR selection.
    await expect(rail.getByRole("checkbox", { name: "projects/ecg and descendants", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Remove Trash filter", exact: true }).click();
    await expect(count(page)).toHaveText("0 tasks");
    await expect(trashNamespace).toBeChecked();
    await expect(trashNamespace.locator("xpath=ancestor::label")).toContainText("0");
    const removeNamespace = page.getByRole("button", { name: `Remove ${trashPath} + descendants filter`, exact: true });
    await expect(removeNamespace).toBeVisible();
    await removeNamespace.click();
    await expect(count(page)).toHaveText("139 tasks");
    await expect(trashNamespace).toHaveCount(0);
    await rail.getByRole("button", { name: "Manage namespaces", exact: true }).click();
    await expect(page.getByRole("region", { name: "Namespace organizer", exact: true }).getByRole("checkbox", { name: trashPath, exact: true })).toBeVisible();
  });

  test("projects stay independent and dedicated details return to the same query and focus", async ({ page }) => {
    await page.getByRole("button", { name: "Projects, any", exact: true }).click();
    await page.getByRole("checkbox", { name: "ECG Research", exact: true }).check();
    await page.keyboard.press("Escape");
    await expect(count(page)).toHaveText("5 tasks");
    await chooseSort(page, "Title");
    const task = rows(page).filter({ hasText: "Improve task browsing" });
    await expect(task).toContainText("Work Buddy");
    await expect(task).toContainText("ECG Research");
    await task.locator(".wb-task-list__select").click();
    await expect(page.getByRole("textbox", { name: "Title", exact: true })).toHaveValue("Improve task browsing");
    await expect(rows(page)).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Linked projects, 2 selected", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Back to tasks", exact: true }).click();
    await expect(count(page)).toHaveText("5 tasks");
    await expect(task.locator(".wb-task-list__select")).toBeFocused();
    await expect(sortField(page)).toContainText("Title");
    expect(new URL(page.url()).searchParams.get("projects")).toBe(String(fixture.tasks.project_ids.ecg));
  });

  test("namespace pills confirm one-task removal and add existing or new paths without changing projects or drafts", async ({ page }) => {
    const before = await taskState(page);
    const taskId = fixture.tasks.task_ids.shared;
    const initial = before.find((task) => task.task_id === taskId)!;
    await page.getByRole("searchbox", { name: "Search tasks", exact: true }).fill("Improve task browsing");
    await expect(rows(page)).toHaveCount(1);
    const row = rows(page).first();
    const remove = row.getByRole("button", { name: "Remove research/analysis from this task", exact: true });
    await expect(row.locator("button button")).toHaveCount(0);
    await expect(row.getByRole("checkbox")).toHaveCount(0);
    await remove.click();
    const confirmation = page.getByRole("alertdialog", { name: "Remove namespace from this task?", exact: true });
    await expect(confirmation).toContainText("only this task’s assignment");
    await expect(confirmation.getByRole("button", { name: "Cancel", exact: true })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(confirmation).toHaveCount(0);
    await expect(remove).toBeFocused();
    expect(await taskState(page)).toEqual(before);
    await remove.click();
    await confirmation.getByRole("button", { name: "Remove namespace", exact: true }).click();
    await expect(remove).toHaveCount(0);
    await expect(row.getByRole("button", { name: "Remove projects/work-buddy/ui from this task", exact: true })).toBeVisible();
    await expect(page.getByRole("textbox", { name: "Title", exact: true })).toHaveCount(0);

    await row.getByRole("button", { name: "Add namespace to this task", exact: true }).click();
    const picker = page.getByRole("dialog", { name: "Add namespace", exact: true });
    await picker.getByRole("searchbox", { name: "Find or create namespace", exact: true }).fill("research");
    await picker.getByRole("button", { name: "Add research to this task", exact: true }).click();
    await expect(picker).toHaveCount(0);
    await row.getByRole("button", { name: "Add namespace to this task", exact: true }).click();
    await picker.getByRole("searchbox", { name: "Find or create namespace", exact: true }).fill("manual/verification");
    await picker.getByRole("button", { name: "Create and add namespace", exact: true }).click();
    await expect(picker).toHaveCount(0);
    await page.reload();
    await expect(row.getByRole("button", { name: "Remove manual/verification from this task", exact: true })).toBeVisible();
    const changed = (await taskState(page)).find((task) => task.task_id === taskId)!;
    expect([...changed.namespaces].sort()).toEqual(["manual/verification", "projects/work-buddy/ui", "research"]);
    expect(changed.project_ids).toEqual(initial.project_ids);

    await titleButtons(page).first().click();
    const title = page.getByRole("textbox", { name: "Title", exact: true });
    await title.fill("Unsaved namespace review title");
    const detailNamespaces = page.getByRole("region", { name: "Task details", exact: true }).getByRole("group", { name: "Task namespaces", exact: true });
    for (const name of ["research", "manual/verification"]) {
      await detailNamespaces.getByRole("button", { name: `Remove ${name} from this task`, exact: true }).click();
      if (name === "research") {
        // Hold the real request at the browser boundary to exercise a pending
        // modal inside the detail view's Escape-to-Back handler.
        let release!: () => void;
        const pending = new Promise<void>((resolve) => { release = resolve; });
        const mutationPath = `/api/tasks/${encodeURIComponent(taskId)}`;
        await page.route(`**${mutationPath}`, async (route) => {
          await pending;
          await route.continue();
        }, { times: 1 });
        const sent = page.waitForRequest((request) => request.method() === "PATCH" && new URL(request.url()).pathname === mutationPath);
        try {
          await confirmation.getByRole("button", { name: "Remove namespace", exact: true }).click();
          await sent;
          await page.keyboard.press("Escape");
          await expect(confirmation).toBeVisible();
          await expect(title).toHaveValue("Unsaved namespace review title");
        } finally {
          release();
        }
      } else {
        await confirmation.getByRole("button", { name: "Remove namespace", exact: true }).click();
      }
      await expect(confirmation).toHaveCount(0);
      await expect(title).toHaveValue("Unsaved namespace review title");
    }
    await detailNamespaces.getByRole("button", { name: "Add namespace to this task", exact: true }).click();
    await picker.getByRole("searchbox", { name: "Find or create namespace", exact: true }).fill("research/analysis");
    await picker.getByRole("button", { name: "Create and add namespace", exact: true }).click();
    await expect(picker).toHaveCount(0);
    await expect(title).toHaveValue("Unsaved namespace review title");
    await title.fill("Improve task browsing");
    const restored = await taskState(page);
    expect([...restored.find((task) => task.task_id === taskId)!.namespaces].sort()).toEqual([...initial.namespaces].sort());
    expect(restored.find((task) => task.task_id === taskId)!.project_ids).toEqual(initial.project_ids);
    expect(restored.filter((task) => task.task_id !== taskId)).toEqual(before.filter((task) => task.task_id !== taskId));
    await page.screenshot({ path: test.info().outputPath("task-namespace-pills-detail.png") });
  });

  test("saved browse state restores on bare entry, explicit URLs win, and resetting the view preserves detail drafts", async ({ page }) => {
    const before = await taskState(page);
    const search = page.getByRole("searchbox", { name: "Search tasks", exact: true });
    await search.fill("Improve task browsing");
    await expect(rows(page)).toHaveCount(1);
    await titleButtons(page).first().click();
    const title = page.getByRole("textbox", { name: "Title", exact: true });
    await title.fill("Detail draft survives the browsing eraser");
    await page.getByRole("button", { name: "Back to tasks", exact: true }).click();
    await search.clear();
    await expect(count(page)).toHaveText("139 tasks");
    await page.getByRole("button", { name: "Projects, any", exact: true }).click();
    await page.getByRole("checkbox", { name: "ECG Research", exact: true }).check();
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await namespaceRail(page).getByRole("checkbox", { name: "projects and descendants", exact: true }).check();
    await chooseSort(page, "Title");
    await page.getByRole("button", { name: "Reverse sort direction, currently A–Z", exact: true }).click();
    await expect(count(page)).toHaveText("2 tasks");
    await expect(page.getByRole("status", { name: "Refreshing…", exact: true })).toHaveCount(0);
    await page.goto(`${baseURL}/app/tasks`);
    await expect(count(page)).toHaveText("2 tasks");
    await expect(namespaceRail(page).getByRole("checkbox", { name: "projects and descendants", exact: true })).toBeChecked();
    await expect(page.getByRole("button", { name: "Projects, 1 selected", exact: true })).toBeVisible();
    await expect(sortField(page)).toContainText("Title");
    await expect(page.getByRole("button", { name: "Reverse sort direction, currently Z–A", exact: true })).toBeVisible();
    await bringWorkspaceIntoView(page);
    await page.screenshot({ path: test.info().outputPath("task-browse-state-restored.png") });

    await page.goto(`${baseURL}/app/tasks?statuses=completed&sort=updated_at&direction=asc`);
    await expect(count(page)).toHaveText("1 task");
    await expect(rows(page).first()).toContainText("Completed comparison analysis");
    await expect(page.getByRole("button", { name: "Projects, any", exact: true })).toBeVisible();
    await expect(namespaceRail(page).getByRole("checkbox", { name: "projects and descendants", exact: true })).not.toBeChecked();
    await expect(search).toHaveValue("");
    await expect(sortField(page)).toContainText("Date updated");
    await expect(page.getByRole("button", { name: "Reverse sort direction, currently Oldest first", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Remove Open filter", exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Remove Completed filter", exact: true })).toBeVisible();
    await search.fill("comparison");
    await namespaceRail(page).getByRole("checkbox", { name: "projects and descendants", exact: true }).check();
    await namespaceRail(page).getByRole("checkbox", { name: "projects and descendants", exact: true }).click();
    await expect(namespaceRail(page).getByRole("checkbox", { name: "projects only", exact: true })).toHaveAttribute("aria-checked", "mixed");
    await expect(count(page)).toHaveText("0 tasks");
    const reset = page.getByRole("button", { name: "Reset Task Workspace view", exact: true });
    await reset.click();
    const resetDialog = page.getByRole("alertdialog", { name: "Reset task view?", exact: true });
    await expect(resetDialog).toContainText("namespace");
    await resetDialog.getByRole("button", { name: "Keep view", exact: true }).click();
    await expect(count(page)).toHaveText("0 tasks");
    await reset.click();
    await page.screenshot({ path: test.info().outputPath("task-browse-reset-confirmation.png") });
    await resetDialog.getByRole("button", { name: "Reset view", exact: true }).click();
    await expect(count(page)).toHaveText("139 tasks");
    await expect(search).toHaveValue("");
    await expect(page.getByRole("button", { name: "Status, 1 selected", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Remove Open filter", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Remove Completed filter", exact: true })).toHaveCount(0);
    await expect(sortField(page)).toContainText("Date created");
    await expect(page.getByRole("button", { name: "Reverse sort direction, currently Newest first", exact: true })).toBeVisible();
    await expect(namespaceRail(page).getByRole("button", { name: "Clear namespace filters", exact: true })).toBeDisabled();
    await expect(page.getByRole("status", { name: "Refreshing…", exact: true })).toHaveCount(0);
    await page.goto(`${baseURL}/app/tasks`);
    await expect(count(page)).toHaveText("139 tasks");
    await expect(rows(page).first()).toContainText("Recently captured task");
    await expect(sortField(page)).toContainText("Date created");
    await search.fill("Improve task browsing");
    await expect(rows(page)).toHaveCount(1);
    await titleButtons(page).first().click();
    await expect(title).toHaveValue("Detail draft survives the browsing eraser");
    await title.fill("Improve task browsing");
    await page.getByRole("button", { name: "Back to tasks", exact: true }).click();
    await search.clear();
    await expect(count(page)).toHaveText("139 tasks");
    expect(await taskState(page)).toEqual(before);
  });

  test("namespace removal cooldown is task-specific, survives list/detail navigation, and safely retries an interrupted response", async ({ page }) => {
    const before = await taskState(page);
    const taskId = fixture.tasks.task_ids.shared;
    const initial = before.find((task) => task.task_id === taskId)!;
    const preferenceKey = `wb.tasks.namespace-confirmations:${encodeURIComponent(taskId)}`;
    const confirmation = page.getByRole("alertdialog", { name: "Remove namespace from this task?", exact: true });
    const skip = confirmation.getByRole("checkbox", { name: "Skip namespace removal confirmations for this task for 10 minutes", exact: true });
    const search = page.getByRole("searchbox", { name: "Search tasks", exact: true });
    const resume = page.getByRole("button", { name: "Resume namespace removal confirmations for this task", exact: true });
    const addNamespace = async (name: string) => {
      await page.getByRole("button", { name: "Add namespace to this task", exact: true }).click();
      const picker = page.getByRole("dialog", { name: "Add namespace", exact: true });
      await picker.getByRole("searchbox", { name: "Find or create namespace", exact: true }).fill(name);
      const existing = picker.getByRole("button", { name: `Add ${name} to this task`, exact: true });
      if (await existing.count()) await existing.click();
      else await picker.getByRole("button", { name: "Create and add namespace", exact: true }).click();
      await expect(picker).toHaveCount(0);
      await expect(page.getByRole("button", { name: `Remove ${name} from this task`, exact: true })).toBeVisible();
    };
    let completions = 0;
    page.on("request", (request) => { if (request.method() === "POST" && new URL(request.url()).pathname.endsWith("/complete")) completions++; });
    await search.fill("Improve task browsing");
    const sharedRow = rows(page).filter({ hasText: "Improve task browsing" });
    await expect(sharedRow).toHaveCount(1);
    await expect(rows(page)).toHaveCount(1);
    await bringWorkspaceIntoView(page);
    const pill = sharedRow.locator(".wb-task-namespace-pills__pill").first();
    const inset = await pill.evaluate((element) => {
      const box = element.getBoundingClientRect();
      const text = element.querySelector("span")!.getBoundingClientRect();
      return text.left - box.left;
    });
    expect(inset).toBeGreaterThanOrEqual(12);
    await page.screenshot({ path: test.info().outputPath("namespace-pill-padding-desktop.png") });
    await page.getByRole("button", { name: "Remove research/analysis from this task", exact: true }).click();
    await expect(skip).not.toBeChecked();
    await skip.check();
    await confirmation.getByRole("button", { name: "Cancel", exact: true }).click();
    expect(await page.evaluate((key) => sessionStorage.getItem(key), preferenceKey)).toBeNull();
    expect(await taskState(page)).toEqual(before);
    await page.getByRole("button", { name: "Remove research/analysis from this task", exact: true }).click();
    await expect(skip).not.toBeChecked();
    await skip.check();
    await page.screenshot({ path: test.info().outputPath("namespace-cooldown-opt-in.png") });
    await confirmation.getByRole("button", { name: "Remove namespace", exact: true }).click();
    await expect(confirmation).toHaveCount(0);
    await expect(resume).toBeVisible();
    const expires = await page.evaluate((key) => Number(sessionStorage.getItem(key)), preferenceKey);
    expect(expires).toBeGreaterThan(Date.now());
    await rows(page).first().getByRole("button", { name: "Complete Improve task browsing", exact: true }).click();
    const completion = page.getByRole("alertdialog", { name: "Complete this task?", exact: true });
    await expect(completion).toBeVisible();
    await completion.getByRole("button", { name: "Cancel", exact: true }).click();
    expect(completions).toBe(0);

    await titleButtons(page).first().click();
    const title = page.getByRole("textbox", { name: "Title", exact: true });
    await title.fill("Cooldown must retain this unsaved title");
    await expect(resume).toBeVisible();
    await addNamespace("research/analysis");
    const directRemove = page.getByRole("button", { name: "Remove projects/work-buddy/ui from this task", exact: true });
    await expect(directRemove).toHaveAttribute("title", "Remove projects/work-buddy/ui from this task immediately.");
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    const mutationPath = `/api/tasks/${encodeURIComponent(taskId)}`;
    await page.route(`**${mutationPath}`, async (route) => {
      await pending;
      const committed = await route.fetch();
      expect(committed.ok()).toBe(true);
      // The synthetic server commits normally; lose only its browser response.
      await route.abort("failed");
    }, { times: 1 });
    const sent = page.waitForRequest((request) => request.method() === "PATCH" && new URL(request.url()).pathname === mutationPath);
    await directRemove.click();
    const originalRequest = (await sent).postDataJSON();
    try {
      await expect(confirmation).toHaveCount(0);
      await expect(page.getByRole("status").filter({ hasText: "Removing projects/work-buddy/ui" })).toBeVisible();
      await expect(title).toHaveValue("Cooldown must retain this unsaved title");
    } finally { release(); }
    const retry = confirmation.getByRole("button", { name: "Retry removal", exact: true });
    await expect(retry).toBeEnabled();
    await page.screenshot({ path: test.info().outputPath("namespace-cooldown-safe-retry.png") });
    const replay = page.waitForRequest((request) => request.method() === "PATCH" && new URL(request.url()).pathname === mutationPath);
    await retry.click();
    expect((await replay).postDataJSON()).toEqual(originalRequest);
    await expect(confirmation).toHaveCount(0);
    await expect(directRemove).toHaveCount(0);
    expect(await page.evaluate((key) => Number(sessionStorage.getItem(key)), preferenceKey)).toBe(expires);
    await expect(title).toHaveValue("Cooldown must retain this unsaved title");
    await addNamespace("projects/work-buddy/ui");
    await page.getByRole("button", { name: "Complete", exact: true }).click();
    await expect(completion).toBeVisible();
    await completion.getByRole("button", { name: "Cancel", exact: true }).click();
    expect(completions).toBe(0);
    await title.fill("Improve task browsing");
    await page.getByRole("button", { name: "Back to tasks", exact: true }).click();
    await page.reload();
    await expect(resume).toBeVisible();
    await page.getByRole("button", { name: "Remove research/analysis from this task", exact: true }).click();
    await expect(page.getByRole("button", { name: "Remove research/analysis from this task", exact: true })).toHaveCount(0);
    await expect(confirmation).toHaveCount(0);
    await addNamespace("research/analysis");
    await search.fill("Check the existing destination namespace");
    await expect(rows(page).filter({ hasText: "Check the existing destination namespace" })).toHaveCount(1);
    await expect(rows(page)).toHaveCount(1);
    await expect(resume).toHaveCount(0);
    await page.getByRole("button", { name: "Remove work-buddy from this task", exact: true }).click();
    await expect(confirmation).toBeVisible();
    await confirmation.getByRole("button", { name: "Cancel", exact: true }).click();
    await search.fill("Improve task browsing");
    await expect(sharedRow).toHaveCount(1);
    await expect(resume).toBeVisible();
    await resume.click();
    await page.getByRole("button", { name: "Remove research/analysis from this task", exact: true }).click();
    await expect(confirmation).toBeVisible();
    await confirmation.getByRole("button", { name: "Cancel", exact: true }).click();
    const after = await taskState(page);
    expect([...after.find((task) => task.task_id === taskId)!.namespaces].sort()).toEqual([...initial.namespaces].sort());
    expect(after.find((task) => task.task_id === taskId)!.project_ids).toEqual(initial.project_ids);
    expect(after.filter((task) => task.task_id !== taskId)).toEqual(before.filter((task) => task.task_id !== taskId));

    const longTitle = "Review a deliberately long task title and a deeply nested namespace without clipping the controls or dates";
    const longNamespace = "research/very-long-namespace-name/long-subtree-name/analysis";
    const longRow = rows(page).filter({ hasText: longTitle });
    const longRemove = longRow.getByRole("button", { name: `Remove ${longNamespace} from this task`, exact: true });
    await search.fill(longTitle);
    // A count of one can still describe the previous result while text search
    // is debouncing. Wait for this task's identity before measuring its pills.
    await expect(longRow).toHaveCount(1);
    await expect(longRemove).toBeVisible();
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(rows(page)).toHaveCount(1);
    await longRow.scrollIntoViewIfNeeded();
    const longPill = longRow.locator(".wb-task-namespace-pills__pill").filter({ has: page.getByRole("button", { name: `Remove ${longNamespace} from this task`, exact: true }) });
    await expect(longPill).toHaveCount(1);
    const pillBox = (await longPill.boundingBox())!;
    const removeBox = (await longRemove.boundingBox())!;
    expect(removeBox.x + removeBox.width).toBeLessThanOrEqual(pillBox.x + pillBox.width + 1);
    expect(pillBox.x + pillBox.width).toBeLessThanOrEqual(390);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: test.info().outputPath("namespace-pill-wrapping-narrow.png") });
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
    await expect(workspace(page).locator(".wb-task-row-checkbox")).toHaveCount(0);
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
    await titleButtons(page).filter({ hasText: title }).click();
    await page.getByRole("button", { name: "Reopen", exact: true }).click();
    await expect(page.getByRole("button", { name: "Complete", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Complete", exact: true }).click();
    await expect(dialog).toContainText(title);
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Complete", exact: true })).toBeFocused();
    expect(completions).toBe(1);
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    await page.route("**/api/tasks/*/complete", async (route) => {
      await pending;
      await route.continue();
    }, { times: 1 });
    const sent = page.waitForRequest((request) => request.method() === "POST" && new URL(request.url()).pathname.endsWith("/complete"));
    await page.getByRole("button", { name: "Complete", exact: true }).click();
    try {
      await dialog.getByRole("button", { name: "Mark complete", exact: true }).click();
      await sent;
      await page.keyboard.press("Escape");
      await expect(dialog).toBeVisible();
      await expect(page.getByRole("textbox", { name: "Title", exact: true })).toHaveValue(title);
    } finally {
      release();
    }
    await expect(dialog).toHaveCount(0);
    expect(completions).toBe(2);
    await page.getByRole("button", { name: "Reopen", exact: true }).click();
    await page.getByRole("button", { name: "Back to tasks", exact: true }).click();
    await page.getByRole("button", { name: "Remove Completed filter", exact: true }).click();
    await expect(count(page)).toHaveText("139 tasks");
  });

  test("review, cancel, apply and durable Undo preserve task and project identity across all statuses", async ({ page }) => {
    const before = await taskState(page);
    await namespaceRail(page).getByRole("button", { name: "Manage namespaces", exact: true }).click();
    const organizer = page.getByRole("region", { name: "Namespace organizer", exact: true });
    await expect(organizer.getByRole("heading", { name: "Manage namespaces", exact: true })).toBeVisible();
    await expect(organizer.getByRole("button", { name: "Preview change", exact: true })).toHaveCount(0);
    const branch = organizer.getByRole("checkbox", { name: "projects", exact: true });
    await expect(branch.locator("xpath=ancestor::label")).toContainText("1 direct · 7 including descendants");
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
    await page.screenshot({ path: test.info().outputPath("narrow-page-default.png") });
    await bringWorkspaceIntoView(page);
    const secondRow = await rows(page).nth(1).boundingBox();
    expect(secondRow!.y + secondRow!.height).toBeLessThanOrEqual(844);
    await page.screenshot({ path: test.info().outputPath("narrow-browse.png") });
    const original = "Improve task browsing";
    await page.getByRole("searchbox", { name: "Search tasks", exact: true }).fill(original);
    await expect(rows(page)).toHaveCount(1);
    await titleButtons(page).first().click();
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
    await titleButtons(page).first().click();
    await expect(title).toHaveValue("Retained workspace edit");
    await page.reload();
    await expect(title).toHaveValue("Retained workspace edit");
    await page.getByRole("button", { name: "Save changes", exact: true }).click();
    await expect(page.getByText("All changes saved", { exact: true })).toBeVisible();
    await page.reload();
    await expect(title).toHaveValue("Retained workspace edit");
    await expect(projects).toBeVisible();
    const namespaces = page.getByRole("region", { name: "Task details", exact: true }).getByRole("group", { name: "Task namespaces", exact: true });
    await expect(namespaces.getByRole("button", { name: "Remove projects/work-buddy/ui from this task", exact: true })).toBeVisible();
    await expect(namespaces.getByRole("button", { name: "Remove research/analysis from this task", exact: true })).toBeVisible();
    await title.fill(original);
    await page.getByRole("button", { name: "Save changes", exact: true }).click();
    await expect(page.getByText("All changes saved", { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
});
