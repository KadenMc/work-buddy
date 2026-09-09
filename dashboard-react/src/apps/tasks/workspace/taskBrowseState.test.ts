import { describe, expect, it } from "vitest";
import { DEFAULT_TASK_BROWSE_STATE, hasTaskBrowseQuery, taskBrowsePatch, taskBrowseState } from "./taskBrowseState";

describe("Task browsing state", () => {
  it("resets all filter dimensions and legacy aliases without resetting a selection or layout", () => {
    expect(taskBrowsePatch(DEFAULT_TASK_BROWSE_STATE)).toEqual({
      statuses: ["open"], projects: [], namespaces: [], exact_namespaces: [], attention: [], urgencies: [],
      q: "", due: "", note: "", sort: "created_at", direction: "desc", mode: "browse", offset: 0, limit: 50,
      lens: null, project: null, namespace: null, urgency: null, state: null,
    });
  });

  it("retains exact paths, unrestricted lifecycle selection and explicit sort direction", () => {
    expect(taskBrowseState({ statuses: [], namespaces: ["old//branch/"], exact_namespaces: ["research"], sort: "title", direction: "desc", offset: 100, task: "task-1", proposal: "th-1234abcd" })).toMatchObject({
      statuses: [], namespaces: ["old//branch/"], exact_namespaces: ["research"], sort: "title", direction: "desc", offset: 100,
    });
    expect(taskBrowseState({ task: "task-1" })).not.toHaveProperty("task");
    expect(taskBrowseState({ mode: "namespaces" }).mode).toBe("browse");
  });

  it.each(["?statuses=", "?exact_namespaces=research", "?sort=title", "?q=", "?lens=completed", "?mode=triage", "?offset=50"])("treats %s as an explicit complete query", (search) => {
    expect(hasTaskBrowseQuery(search)).toBe(true);
  });
  it.each(["", "?task=task-1", "?proposal=th-1234abcd", "?mode=namespaces", "?unrelated=value"])("restores saved browsing around %s", (search) => {
    expect(hasTaskBrowseQuery(search)).toBe(false);
  });
});
