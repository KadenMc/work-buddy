import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  UNSAVED_WORK_PROMPT,
  anyDirty,
  confirmDiscardUnsavedWork,
  guardedNavigate,
  useUnsavedWorkGuard,
} from "./routeGuard";

describe("anyDirty", () => {
  it("is true when any signal is set", () => {
    expect(anyDirty(false, false)).toBe(false);
    expect(anyDirty(false, true)).toBe(true);
    expect(anyDirty(true, false)).toBe(true);
    expect(anyDirty()).toBe(false);
  });
});

function GuardHarness({ getDirty }: { getDirty: () => boolean }) {
  useUnsavedWorkGuard(getDirty, true);
  return null;
}

describe("useUnsavedWorkGuard", () => {
  it("prevents unload only while unsaved work is present", () => {
    let dirty = false;
    render(<GuardHarness getDirty={() => dirty} />);

    const clean = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(clean);
    expect(clean.defaultPrevented).toBe(false);

    dirty = true;
    const staged = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(staged);
    expect(staged.defaultPrevented).toBe(true);
  });

  it("reads the live union at event time, covering either dirty source", () => {
    let marksDirty = false;
    let chatDirty = false;
    render(
      <GuardHarness getDirty={() => anyDirty(marksDirty, chatDirty)} />,
    );

    chatDirty = true;
    const onlyChat = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(onlyChat);
    expect(onlyChat.defaultPrevented).toBe(true);
  });
});

describe("confirmDiscardUnsavedWork", () => {
  it("proceeds without prompting when there is no unsaved work", () => {
    const confirmImpl = vi.fn(() => false);
    expect(confirmDiscardUnsavedWork(false, { confirmImpl })).toBe(true);
    expect(confirmImpl).not.toHaveBeenCalled();
  });

  it("prompts with the discard message and honors the answer", () => {
    const yes = vi.fn(() => true);
    expect(confirmDiscardUnsavedWork(true, { confirmImpl: yes })).toBe(true);
    expect(yes).toHaveBeenCalledWith(UNSAVED_WORK_PROMPT);

    const no = vi.fn(() => false);
    expect(confirmDiscardUnsavedWork(true, { confirmImpl: no })).toBe(false);
  });
});

describe("guardedNavigate", () => {
  it("navigates immediately when clean", () => {
    const navigate = vi.fn();
    const proceeded = guardedNavigate(navigate, "/app/other", false);
    expect(proceeded).toBe(true);
    expect(navigate).toHaveBeenCalledWith("/app/other");
  });

  it("blocks the navigation when unsaved work is present and discard is declined", () => {
    const navigate = vi.fn();
    const proceeded = guardedNavigate(navigate, "/app/other", true, {
      confirmImpl: () => false,
    });
    expect(proceeded).toBe(false);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("navigates when the human confirms the discard", () => {
    const navigate = vi.fn();
    const proceeded = guardedNavigate(navigate, "/app/other", true, {
      confirmImpl: () => true,
    });
    expect(proceeded).toBe(true);
    expect(navigate).toHaveBeenCalledWith("/app/other");
  });
});
