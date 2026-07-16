import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SystemStatusClient } from "./client";
import { ControlStatusPage } from "./ControlStatusPage";
import type { ControlGraphNode, ControlGraphSnapshot } from "./contracts";

function node(
  overrides: Partial<ControlGraphNode> & Pick<ControlGraphNode, "id" | "label">,
): ControlGraphNode {
  const { id, label, ...rest } = overrides;
  return {
    id,
    kind: "component",
    label,
    description: "",
    grouping_parents: [],
    preference: "wanted",
    effective_state: "ok",
    component_id: id.replace("component:", ""),
    requirement_ids: [],
    status_reason: "Ready",
    blocking_issues: [],
    fix_kind: "none",
    fix_params: {},
    fix_preview: null,
    ...rest,
  };
}

const snapshot: ControlGraphSnapshot = {
  nodes: {
    "component:obsidian": node({
      id: "component:obsidian",
      label: "Obsidian",
      effective_state: "unconfigured",
      status_reason: "Vault path is missing.",
      requirement_ids: ["req:obsidian/vault"],
    }),
    "req:obsidian/vault": node({
      id: "req:obsidian/vault",
      kind: "requirement",
      label: "Choose a vault",
      component_id: null,
      effective_state: "unconfigured",
      status_reason: "A path is required.",
      fix_kind: "input_required",
    }),
    "component:calendar": node({
      id: "component:calendar",
      label: "Calendar",
      effective_state: "degraded",
      status_reason: "Provider is unavailable.",
    }),
    "component:telegram": node({
      id: "component:telegram",
      label: "Telegram",
      preference: "unwanted",
      effective_state: "disabled",
      status_reason: "Disabled by preference.",
      requirement_ids: ["req:telegram/token"],
    }),
    "req:telegram/token": node({
      id: "req:telegram/token",
      kind: "requirement",
      label: "Telegram token",
      component_id: null,
      effective_state: "disabled",
      status_reason: "Telegram is disabled.",
      fix_kind: "programmatic",
    }),
    "component:core": node({
      id: "component:core",
      label: "Core runtime",
      preference: "required",
    }),
  },
};

function client(): SystemStatusClient {
  return {
    load: vi.fn().mockResolvedValue(snapshot),
    reprobe: vi.fn().mockResolvedValue(snapshot),
    setComponentWanted: vi.fn().mockResolvedValue(snapshot),
    repair: vi.fn().mockResolvedValue({ ok: true }),
    requestHelp: vi.fn().mockResolvedValue({ ok: true }),
  };
}

describe("ControlStatusPage", () => {
  it("projects the control graph into task-oriented status buckets", async () => {
    render(<ControlStatusPage client={client()} />);

    expect(await screen.findByRole("heading", { name: "Status & repairs" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Needs setup: 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Needs attention: 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disabled by you: 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Healthy: 1" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Obsidian" })).toBeInTheDocument();
    expect(screen.getByText("A path is required.")).toBeInTheDocument();
  });

  it("uses the existing control APIs for reprobe, help, and preferences", async () => {
    const api = client();
    render(<ControlStatusPage client={api} />);
    await screen.findByRole("heading", { name: "Obsidian" });

    fireEvent.click(screen.getByRole("button", { name: "Recheck all" }));
    await waitFor(() => expect(api.reprobe).toHaveBeenCalledOnce());
    expect(await screen.findByRole("status")).toHaveTextContent(
      "System checks are up to date.",
    );

    fireEvent.click(screen.getByRole("button", { name: /Disabled by you/i }));
    expect(screen.queryByRole("button", { name: "Repair" })).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Enable" }));
    await waitFor(() =>
      expect(api.setComponentWanted).toHaveBeenCalledWith("telegram", true),
    );

    fireEvent.click(screen.getByRole("button", { name: /Needs attention/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Get help" }));
    await waitFor(() =>
      expect(api.requestHelp).toHaveBeenCalledWith("component:calendar"),
    );
  });

  it("announces failed system actions assertively", async () => {
    const api = client();
    vi.mocked(api.reprobe).mockRejectedValueOnce(new Error("Probe service offline"));
    render(<ControlStatusPage client={api} />);
    await screen.findByRole("heading", { name: "Obsidian" });

    fireEvent.click(screen.getByRole("button", { name: "Recheck all" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Probe service offline",
    );
  });

  it("keeps status inspectable but disables every side effect in read-only mode", async () => {
    const api = client();
    render(
      <ControlStatusPage
        client={{
          ...api,
          load: vi.fn().mockResolvedValue({ ...snapshot, read_only: true }),
        }}
      />,
    );

    expect(await screen.findByText(/This dashboard is read-only/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Recheck all" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Disable" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /Needs attention/i }));
    expect(await screen.findByRole("button", { name: "Get help" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /Disabled by you/i }));
    expect(await screen.findByRole("button", { name: "Enable" })).toBeDisabled();
    expect(api.reprobe).not.toHaveBeenCalled();
    expect(api.setComponentWanted).not.toHaveBeenCalled();
    expect(api.requestHelp).not.toHaveBeenCalled();
    expect(api.repair).not.toHaveBeenCalled();
  });
});
