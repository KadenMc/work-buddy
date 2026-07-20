import { describe, expect, it, vi } from "vitest";

import { RailStore, isDirty } from "./store";

describe("RailStore", () => {
  it("starts clean with the default resting state", () => {
    const store = new RailStore();
    const state = store.getState();
    expect(state.tab).toBe("review");
    expect(state.mode).toBe("stream");
    expect(state.filter).toBe("all");
    expect(isDirty(state)).toBe(false);
  });

  it("notifies subscribers on a real change and skips a no-op", () => {
    const store = new RailStore();
    const listener = vi.fn();
    store.subscribe(listener);
    store.setMode("queue");
    expect(listener).toHaveBeenCalledTimes(1);
    // Clearing an absent decision is a no-op and must not notify.
    store.clearDecision("missing");
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("stages and clears a proposal decision, tracking dirtiness", () => {
    const store = new RailStore();
    store.stageDecision({
      proposalId: "p1",
      verb: "confirm",
      canonicalSha256: "hash",
    });
    expect(isDirty(store.getState())).toBe(true);
    expect(store.getState().decisions.p1.verb).toBe("confirm");
    store.clearDecision("p1");
    expect(isDirty(store.getState())).toBe(false);
  });

  it("stages a claim decision and clears all", () => {
    const store = new RailStore();
    store.stageClaimDecision({
      claimId: "c1",
      verb: "confirm",
      canonicalSha256: "h",
    });
    expect(isDirty(store.getState())).toBe(true);
    store.clearAllDecisions();
    expect(isDirty(store.getState())).toBe(false);
  });

  it("resets the queue cursor when the filter changes", () => {
    const store = new RailStore({ queueIndex: 4 });
    store.setFilter("flags");
    expect(store.getState().queueIndex).toBe(0);
    expect(store.getState().filter).toBe("flags");
  });

  it("tracks selection and the inspector span", () => {
    const store = new RailStore();
    store.select("p1", "proposal");
    expect(store.getState().selectedId).toBe("p1");
    expect(store.getState().selectedKind).toBe("proposal");
    store.openInspector("sp-1");
    expect(store.getState().inspectorSpanId).toBe("sp-1");
    store.closeInspector();
    expect(store.getState().inspectorSpanId).toBeNull();
  });

  it("invokes onTabChange with the new tab when one is provided", () => {
    const onTabChange = vi.fn();
    const store = new RailStore({}, { onTabChange });
    store.setTab("chat");
    expect(onTabChange).toHaveBeenCalledTimes(1);
    expect(onTabChange).toHaveBeenCalledWith("chat");
    expect(store.getState().tab).toBe("chat");
  });

  it("changes the tab safely without an onTabChange callback", () => {
    const store = new RailStore();
    store.setTab("chat");
    expect(store.getState().tab).toBe("chat");
  });

  it("hydrates a persisted draft", () => {
    const store = new RailStore();
    store.hydrateDecisions(
      { p1: { proposalId: "p1", verb: "defer", canonicalSha256: "h" } },
      {},
    );
    expect(store.getState().decisions.p1.verb).toBe("defer");
    expect(isDirty(store.getState())).toBe(true);
  });
});
