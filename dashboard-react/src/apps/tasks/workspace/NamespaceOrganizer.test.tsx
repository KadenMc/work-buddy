import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { IntentResult, JsonValue } from "../../../dashboard/contributions/contracts";
import { TASK_INTENTS } from "../contracts";
import { DashboardHelpProvider } from "../../../dashboard/help";
import { NamespaceOrganizer } from "./NamespaceOrganizer";

const nodes = [
  { path: "research", parent: null, label: "research", count: 3, direct_count: 1 },
  { path: "research/ecg", parent: "research", label: "ecg", count: 2, direct_count: 2 },
  { path: "ecg", parent: null, label: "ecg", count: 1, direct_count: 1 },
];
const operation = { operation_id: "op-1", action: "rename", label: "Renamed research", created_at: "2026-09-09T12:00:00Z", undone_at: null, can_undo: true };
const preview = {
  request: { action: "rename", sources: ["research"], name: "science", include_descendants: true, merge_collisions: false },
  fingerprint: "reviewed-fingerprint", collection_revision: 2, action: "rename",
  mapping: [{ from: "research", to: "science", task_count: 1, collision: false }, { from: "research/ecg", to: "science/ecg", task_count: 2, collision: false }],
  task_count: 3, assignment_count: 3, status_counts: { open: 1, completed: 1, archived: 1, trash: 0 },
  unnamespaced_count: 0, collisions: [], can_apply: true, issues: [], scope: "all_statuses",
  tasks: [{ task_id: "t-1", title: "Task one", revision: 1, before: ["research/ecg"], after: ["science/ecg"] }], tasks_offset: 0, tasks_limit: 25, tasks_has_more: false,
};
const accepted = (value: unknown): IntentResult => ({ intent_id: "test", status: "accepted", value: value as JsonValue });

describe("NamespaceOrganizer", () => {
  it("explains preview, apply and Undo in Help mode without triggering their effects on hover", async () => {
    const user = userEvent.setup();
    const send = vi.fn(async (type: string) => type === TASK_INTENTS.namespaceLoad ? accepted({ namespaces: nodes, operations: [] }) : type === TASK_INTENTS.namespacePreview ? accepted({ preview }) : accepted({ operation }));
    render(<DashboardHelpProvider enabled><NamespaceOrganizer send={send} readOnly={false} onClose={vi.fn()} onApplied={vi.fn()} /></DashboardHelpProvider>);
    await user.click(await screen.findByRole("checkbox", { name: "research" }));
    await user.hover(screen.getByRole("button", { name: "Rename" }));
    expect(await screen.findByRole("tooltip", {}, { timeout: 3000 })).toHaveTextContent("preview affected assignments");
    expect(send.mock.calls.every(([type]) => type === TASK_INTENTS.namespaceLoad)).toBe(true);
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Rename" }));
    await user.hover(screen.getByRole("button", { name: "Preview change" }));
    expect(await screen.findByRole("tooltip", {}, { timeout: 3000 })).toHaveTextContent("without writing it");
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Preview change" }));
    await user.hover(await screen.findByRole("button", { name: "Rename · 3 tasks" }));
    expect(await screen.findByRole("tooltip", {}, { timeout: 3000 })).toHaveTextContent("including completed, archived, and trashed tasks");
    expect(send.mock.calls.some(([type]) => type === TASK_INTENTS.namespaceApply)).toBe(false);
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Rename · 3 tasks" }));
    await user.hover(await screen.findByRole("button", { name: "Undo renamed research" }));
    expect(await screen.findByRole("tooltip", {}, { timeout: 3000 })).toHaveTextContent("subsequent edits have not made that unsafe");
    expect(send.mock.calls.filter(([type]) => type === TASK_INTENTS.namespaceApply)).toHaveLength(1);
    expect(send.mock.calls.some(([type]) => type === TASK_INTENTS.namespaceUndo)).toBe(false);
  });

  it("starts with an inventory, preserves a rename through preview/back, and cancels without writing", async () => {
    const user = userEvent.setup();
    const send = vi.fn(async (type: string) => type === TASK_INTENTS.namespaceLoad ? accepted({ namespaces: nodes, operations: [] }) : accepted({ preview }));
    render(<NamespaceOrganizer send={send} readOnly={false} onClose={vi.fn()} onApplied={vi.fn()} />);
    await screen.findByText("research/ecg");
    expect(screen.queryByRole("textbox", { name: "New name" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "research" }));
    await user.click(screen.getByRole("button", { name: "Rename" }));
    const name = screen.getByRole("textbox", { name: "New name" });
    await user.clear(name); await user.type(name, "science");
    await user.click(screen.getByRole("button", { name: "Preview change" }));
    expect(await screen.findByRole("table", { name: "Namespace changes" })).toBeInTheDocument();
    expect(screen.getByText(/1 completed/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Inspect affected tasks" }));
    expect(screen.getByText("Task one")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Back to edit" }));
    expect(screen.getByRole("textbox", { name: "New name" })).toHaveValue("science");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(send.mock.calls.some(([type]) => type === TASK_INTENTS.namespaceApply)).toBe(false);
  });

  it("retries an uncertain apply with the same reviewed request and idempotency key, then offers Undo", async () => {
    const user = userEvent.setup(); let attempts = 0;
    const send = vi.fn(async (type: string, _body: JsonValue, _mutation?: boolean, _id?: string) => {
      if (type === TASK_INTENTS.namespaceLoad) return accepted({ namespaces: nodes, operations: attempts > 1 ? [operation] : [] });
      if (type === TASK_INTENTS.namespacePreview) return accepted({ preview });
      if (type === TASK_INTENTS.namespaceApply) { attempts++; if (attempts === 1) throw new Error("Connection interrupted"); return accepted({ operation }); }
      return accepted({ operation: { ...operation, can_undo: false, undone_at: "now" } });
    });
    render(<NamespaceOrganizer send={send} readOnly={false} onClose={vi.fn()} onApplied={vi.fn()} />);
    await user.click(await screen.findByRole("checkbox", { name: "research" }));
    await user.click(screen.getByRole("button", { name: "Rename" }));
    await user.click(screen.getByRole("button", { name: "Preview change" }));
    await user.click(await screen.findByRole("button", { name: "Rename · 3 tasks" }));
    expect(await screen.findByText("Connection interrupted")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry same change" }));
    await waitFor(() => expect(attempts).toBe(2));
    const applies = send.mock.calls.filter(([type]) => type === TASK_INTENTS.namespaceApply);
    expect(applies[0]).toEqual(applies[1]);
    expect(applies[0]?.[1]).toEqual({ request: preview.request, expected_fingerprint: preview.fingerprint });
    await user.click((await screen.findAllByRole("button", { name: "Undo renamed research" }))[0]!);
    await waitFor(() => expect(send).toHaveBeenCalledWith(TASK_INTENTS.namespaceUndo, { operation_id: "op-1" }, true, expect.any(String)));
  });

  it("requires an explicit choice for direct parent assignments before promoting children", async () => {
    const user = userEvent.setup();
    const send = vi.fn(async () => accepted({ namespaces: nodes, operations: [] }));
    render(<NamespaceOrganizer send={send} readOnly={false} onClose={vi.fn()} onApplied={vi.fn()} />);
    await user.click(await screen.findByRole("checkbox", { name: "research" }));
    await user.click(screen.getByRole("button", { name: "Move children up one level" }));
    expect(screen.getByRole("button", { name: "Preview change" })).toBeDisabled();
    await user.click(screen.getByRole("radio", { name: "Keep assignments on this namespace" }));
    expect(screen.getByRole("button", { name: "Preview change" })).toBeEnabled();
    expect(send.mock.calls).toHaveLength(1);
  });

  it("supports selected task Replace with an explicit empty assignment warning", async () => {
    const user = userEvent.setup();
    const send = vi.fn(async () => accepted({ namespaces: nodes, operations: [] }));
    render(<NamespaceOrganizer send={send} readOnly={false} selectedTaskIds={["t-1", "t-2"]} onClose={vi.fn()} onApplied={vi.fn()} />);
    await user.click(screen.getByRole("radio", { name: "Replace namespaces" }));
    expect(screen.getByText(/Replacing with no namespaces removes all/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Preview change" }));
    await waitFor(() => expect(send).toHaveBeenCalledWith(TASK_INTENTS.namespacePreview, expect.objectContaining({ action: "assign", task_ids: ["t-1", "t-2"], assignment_mode: "replace", namespaces: [] })));
  });
});
