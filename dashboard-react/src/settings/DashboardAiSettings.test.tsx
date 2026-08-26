import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DashboardHelpProvider } from "../dashboard/help";
import { TypographyScaleProvider } from "../theme/TypographyScaleProvider";
import { SettingsPage } from "./SettingsPage";
import { nativeSettingsRegistry } from "./nativeContributions";

const setting = "wb.dashboard.chat-execution-default";
const sonnet = { provider_id: "claude-code", model_id: "sonnet" };
const codex = { provider_id: "codex", model_id: "fixture-model" };
const catalog = { providers: [
  { id: "claude-code", label: "Claude Code", available: true, models: [{ id: "sonnet", label: "Sonnet", available: true }] },
  { id: "codex", label: "Codex", available: true, models: [{ id: "fixture-model", label: "Fixture model", available: true }] },
] };
const record = (pair = sonnet, revision = "value:0") => ({ setting_id: setting, scope: { kind: "profile" }, effective_value: pair, source: revision === "value:0" ? "default" : "profile", is_modified: revision !== "value:0", revision });

function renderPage(fetchImpl: typeof fetch, help = false) {
  vi.stubGlobal("fetch", fetchImpl);
  return render(<MemoryRouter initialEntries={["/settings/system/dashboard-ai"]}>
    <TypographyScaleProvider initialScale="standard"><DashboardHelpProvider enabled={help}>
      <SettingsPage registryOverride={nativeSettingsRegistry} />
    </DashboardHelpProvider></TypographyScaleProvider>
  </MemoryRouter>);
}

function mockServer(fail = false, readOnly = false) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/settings/values?")) return Response.json({ schema_version: 1, registry_revision: "settings-registry:6", observed_at: "2026-08-26T00:00:00Z", read_only: readOnly, values: [record(), { setting_id: "wb.dashboard.assistance", scope: { kind: "profile" }, effective_value: "disabled", source: "default", is_modified: false, revision: "value:0" }] });
    if (url === "/api/settings/execution-catalog") return Response.json(catalog);
    if (url === `/api/settings/values/${setting}`) {
      if (fail) return Response.json({ error: "provider_unavailable", message: "Provider unavailable. Try again." }, { status: 503 });
      if (init?.method === "PATCH") {
        expect(JSON.parse(String(init.body))).toEqual({ scope: "profile", value: codex, expected_revision: "value:0" });
        return Response.json({ schema_version: 1, registry_revision: "settings-registry:6", value: record(codex, "value:1") });
      }
      expect(init?.method).toBe("DELETE");
      expect(JSON.parse(String(init?.body))).toEqual({ scope: "profile", expected_revision: "value:1" });
      return Response.json({ schema_version: 1, registry_revision: "settings-registry:6", value: { ...record(sonnet, "value:2"), source: "default", is_modified: false } });
    }
    return new Response(null, { status: 404 });
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("Dashboard AI settings", () => {
  it.each([false, true])("keeps the material Form assistance disclosure visible and optional prose contextual (Help: %s)", async (help) => {
    const user = userEvent.setup();
    const fetcher = mockServer();
    renderPage(fetcher, help);
    await screen.findByRole("button", { name: "Run with Claude Code · Sonnet" });
    const heading = screen.getByRole("heading", { name: "Form assistance" });
    const card = heading.closest("article")!;
    const disclosure = "Only Start authorizes sharing the disclosed form and chat with your selected model; submission stays yours.";
    expect(within(card).getByText(disclosure)).toBeVisible();
    expect(screen.queryByText(/Assistance is off by default/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Each Start shows the provider\/model and disclosure limits/)).not.toBeInTheDocument();
    if (help) {
      expect(heading).toHaveAttribute("data-help-target", "true");
      await user.tab();
      act(() => heading.focus());
      const tooltip = await screen.findByRole("tooltip");
      expect(tooltip).toHaveTextContent("Enabling it does not start a session or send data.");
      expect(tooltip).toHaveTextContent("later Send includes the form snapshot for that turn.");
      expect(tooltip).toHaveTextContent("conditionally undone");
      await user.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("tooltip")).not.toBeInTheDocument());
      expect(screen.queryByText(/Assistance is off by default/)).not.toBeInTheDocument();
    } else {
      expect(heading).not.toHaveAttribute("data-help-target");
    }
    expect(within(card).getByText(disclosure)).toBeVisible();
    expect(fetcher.mock.calls.every(([, init]) => init?.method === undefined)).toBe(true);
  });

  it("places Dashboard AI once under System and keeps local-model explanation contextual", async () => {
    const user = userEvent.setup();
    const fetcher = mockServer();
    renderPage(fetcher, true);
    const link = screen.getByRole("link", { name: "Dashboard AI" });
    expect(link).toHaveAttribute("href", "/settings/system/dashboard-ai");
    const section = link.closest("section")!;
    expect(within(section).getByText("System")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Dashboard AI" })).toHaveLength(1);
    expect(screen.queryByText("Assistant model tier")).not.toBeInTheDocument();
    expect(screen.queryByText(/Local inference is a separate/)).not.toBeInTheDocument();
    await screen.findByRole("button", { name: "Run with Claude Code · Sonnet" });
    const modelHeading = screen.getByRole("heading", { name: "Default chat model" });
    expect(modelHeading).toHaveAttribute("data-help-target", "true");
    await user.tab();
    act(() => modelHeading.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Local inference is a separate subsystem");
    expect(fetcher.mock.calls.every(([, init]) => init?.method === undefined)).toBe(true);
  });

  it("uses the shared picker for a Settings-only selection and frozen reset", async () => {
    const user = userEvent.setup();
    const fetcher = mockServer();
    renderPage(fetcher);
    await user.click(await screen.findByRole("button", { name: "Run with Claude Code · Sonnet" }));
    await user.click(screen.getByRole("option", { name: "Codex, Fixture model" }));
    await screen.findByRole("button", { name: "Run with Codex · Fixture model" });
    const reset = screen.getByRole("button", { name: "Reset default chat model" });
    await waitFor(() => expect(reset).toBeEnabled());
    await user.click(reset);
    await screen.findByRole("button", { name: "Run with Claude Code · Sonnet" });
    expect(fetcher.mock.calls.filter(([, init]) => init?.method).map(([, init]) => init?.method)).toEqual(["PATCH", "DELETE"]);
    expect(fetcher.mock.calls.some(([url]) => String(url).includes("conversation"))).toBe(false);
  });

  it("does not announce success for a failed Settings write", async () => {
    const user = userEvent.setup();
    renderPage(mockServer(true));
    await user.click(await screen.findByRole("button", { name: "Run with Claude Code · Sonnet" }));
    await user.click(screen.getByRole("option", { name: "Codex, Fixture model" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Provider unavailable. Try again.");
    expect(screen.getByRole("button", { name: "Run with Claude Code · Sonnet" })).toBeInTheDocument();
    expect(screen.queryByText(/Now using Codex/)).not.toBeInTheDocument();
  });

  it("shows truthful noninteractive model metadata when Settings is read-only", async () => {
    renderPage(mockServer(false, true));
    await screen.findByLabelText("Run with Claude Code · Sonnet");
    expect(screen.queryByRole("button", { name: "Run with Claude Code · Sonnet" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reset default chat model" })).toBeDisabled();
  });
});
