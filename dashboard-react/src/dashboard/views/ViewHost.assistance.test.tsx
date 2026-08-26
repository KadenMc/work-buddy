import { webcrypto } from "node:crypto";
import { useEffect } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider } from "../../theme/ThemeProvider";
import { DashboardTestRuntime } from "../../test/DashboardTestRuntime";
import { DashboardAnnouncer } from "../accessibility/DashboardAnnouncer";
import {
  AssistDraftButton,
  AssistedDraftRuntimeProvider,
  useAssistedDraft,
} from "../assistance";
import type { AssistanceAvailability, AssistanceSession } from "../assistance/contracts";
import { assistedDraftDeclaration } from "../assistance/schema";
import {
  asAppId,
  asViewId,
  asWidgetInstanceId,
  asWidgetModuleId,
  asWidgetRoleId,
  asWidgetSlotId,
  asWidgetTypeId,
  type AppContribution,
  type AppInvalidation,
  type WidgetDefinition,
  type WidgetRendererProps,
} from "../contributions/contracts";
import { ContributionRegistry } from "../contributions/registry";
import { CustomizeModeProvider } from "../customize";
import { CustomizeViewToggle } from "../customize/CustomizeViewToggle";
import { useWidgetDraft } from "../drafts";
import { DashboardEventProvider } from "../events/DashboardEventProvider";
import { InMemoryPersonalizationRepository } from "../personalization/repository";
import type { ViewProvider } from "../providers/ViewProvider";
import { ViewHost } from "./ViewHost";

vi.mock("../../security/humanAuthority", async (original) => ({
  ...await original<typeof import("../../security/humanAuthority")>(),
  exactHumanAuthorityHeaders: vi.fn(async () => ({ "X-Test-Authority": "exact-action" })),
}));

const appId = asAppId("example.assisted-view");
const viewId = asViewId("example.assisted-view.main");
const widgetTypeId = asWidgetTypeId("example.assisted-view.form");
const instanceId = asWidgetInstanceId("default:assisted-form");
const roleId = asWidgetRoleId("example.assisted-view.form@1");
const slotId = asWidgetSlotId("form");
const declaration = assistedDraftDeclaration("task-create");
const mounted = vi.fn();
const unmounted = vi.fn();
const initialDraft = { title: "Original title", summary: "", next_action: "", batch_lines: [] };

function AssistedForm({ presentation }: WidgetRendererProps) {
  const draft = useWidgetDraft("task-create", initialDraft);
  const assistance = useAssistedDraft("task-create", draft, {
    title: "Help with this form",
    interactionMode: presentation.interactionMode,
  });
  useEffect(() => {
    mounted();
    return () => { unmounted(); };
  }, []);
  return (
    <section aria-label="Assisted form fields">
      <label>
        Form title
        <input
          {...assistance.fieldProps(["title"])}
          value={draft.value.title}
          onChange={(event) => draft.setValue({ ...draft.value, title: event.target.value })}
        />
      </label>
      <output aria-label="Draft scope">{draft.identity.scopeKey}</output>
      <AssistDraftButton assistance={assistance} />
    </section>
  );
}

// Deliberately not durable: keeping its DOM must not grant live Operate mode
// during Arrange/Preview, or disable the normal hide/remove controls.
const widget: WidgetDefinition = {
  typeId: widgetTypeId,
  definitionVersion: 1,
  publisherAppId: appId,
  displayName: "Assisted form",
  description: "A non-durable form with an active assistance binding.",
  libraryPath: ["Forms", "Assisted form"],
  providesRoles: [roleId],
  settingsSchema: { schemaId: `${widgetTypeId}.settings`, version: 1 },
  inputSchema: { schemaId: `${widgetTypeId}.input`, version: 1 },
  outputIntentSchemas: [],
  assistableDrafts: [declaration],
  drafts: [{
    draftName: "task-create",
    schema: declaration.schema,
    persistence: "device",
    sensitivity: "ordinary",
    clearPolicy: "confirm",
    maxBytes: 32_768,
    scope: { kind: "input-field", path: ["recordId"] },
  }],
  sizeContract: {
    default: { w: 24, h: 10 }, min: { w: 6, h: 4 },
    modes: ["compact", "standard", "expanded"],
  },
  multiplicity: "single_per_view",
  rendererModuleId: asWidgetModuleId(`${widgetTypeId}.renderer`),
  theme: {
    contractVersion: 1,
    conformance: "standard",
    supports: ["light", "dark", "forced-colors", "reduced-motion"],
    styling: "host-primitives",
  },
};

const contribution: AppContribution = {
  schemaVersion: 1,
  appId,
  definitionVersion: 1,
  displayName: "Assisted View",
  widgetRoles: [{ roleId, ownerAppId: appId, displayName: "Form", description: "Test form" }],
  widgetDefinitions: [widget],
  views: [{
    viewId, definitionVersion: 1, ownerAppId: appId,
    displayName: "Assisted View", route: "assisted-view",
    navigation: { label: "Assisted View", order: 1 },
    primaryJob: "Keep a form and its assistant bound across responsive layouts.",
    grid: { columns: 24 },
    defaultSlots: [{
      slotId, defaultInstanceId: instanceId, requiredRole: roleId,
      defaultWidgetTypeId: widgetTypeId, presence: "default_on",
      help: { summary: "An assisted form.", details: "Only a human submits it." },
      defaultSettings: {}, defaultLayout: { x: 0, y: 0, w: 24, h: 10 },
    }],
    readingOrder: [slotId], mobileOrder: [slotId],
  }],
};
const registry = new ContributionRegistry();
registry.registerApp(contribution, [{
  moduleId: widget.rendererModuleId, widgetTypeId,
  load: async () => ({ default: AssistedForm }),
}]);

function viewProvider() {
  let revision = 1;
  let recordId = "first";
  let invalidate: ((value: AppInvalidation) => void) | undefined;
  const snapshot = () => ({
    viewId, revision, observedAt: "2026-08-26T12:00:00Z", status: "ready" as const,
    quality: { kind: "complete" as const }, model: {}, bindings: {}, widgetInputs: {},
  });
  const provider: ViewProvider = {
    appId,
    subscribeInvalidations: (listener) => {
      invalidate = listener;
      return () => { invalidate = undefined; };
    },
    getAddableWidgetTypeIds: () => [],
    loadView: async () => snapshot(),
    loadWidget: async (type, request) => ({
      widgetTypeId: type, instanceId: request.instanceId, revision,
      observedAt: "2026-08-26T12:00:00Z", status: "ready",
      quality: { kind: "complete" }, input: { recordId },
    }),
    dispatch: async (intent) => ({ intent_id: intent.intent_id, status: "unavailable" }),
    reconcile: async () => ({ changed: true, snapshot: snapshot() }),
  };
  return {
    provider,
    rebind: (next: string) => {
      recordId = next;
      revision += 1;
      invalidate?.({ id: `rebind-${revision}`, appId, viewIds: [viewId], revision, reason: "record changed", observedAt: "2026-08-26T12:01:00Z" });
    },
  };
}

function assistanceBroker() {
  const calls: { path: string; body?: Record<string, unknown> }[] = [];
  let session: AssistanceSession | undefined;
  const availability: AssistanceAvailability = {
    available: true, code: "ready", purpose: "dashboard.assisted_draft",
    message: "Ready", disclosure: "Only explicitly shared form fields reach the model.",
  };
  const response = (value: unknown) => ({ ok: true, status: 200, json: async () => value }) as Response;
  const fetchImpl = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : undefined;
    calls.push({ path, body });
    if (path.endsWith("/availability")) return response(availability);
    if (path === "/api/assistance/sessions") {
      session = {
        protocol: "wb.assisted-draft.session/v2", assistantSessionId: "as-view-test",
        conversationId: "conversation-view-test", phase: "prepared", activeStartId: null,
        controlRevision: 0, identity: body!.identity as AssistanceSession["identity"],
        schema: body!.schema as AssistanceSession["schema"], expiresAt: "2099-01-01T00:00:00Z",
        availability,
        execution: {
          selection: { providerId: "fixture", modelId: "fixture-model", providerLabel: "Fixture", modelLabel: "Model", revision: "execution:1" },
          providers: [{ id: "fixture", label: "Fixture", available: true, models: [{ id: "fixture-model", label: "Model", available: true }] }],
        },
        agent: { status: "not_started", phase: "prepared", activeStartId: null, controlRevision: 0 },
      };
      return response(session);
    }
    if (path.endsWith("/execution")) return response({ execution: session!.execution, agent: session!.agent });
    if (path.endsWith("/start")) {
      session = { ...session!, phase: "active", activeStartId: String(body!.requestId), controlRevision: 1, agent: { status: "running", phase: "active", alive: true, activeStartId: String(body!.requestId), controlRevision: 1 } };
      return response(session);
    }
    if (path.endsWith("/stop")) {
      session = { ...session!, phase: "stopped", activeStartId: null, controlRevision: 2, agent: { status: "stopped", phase: "stopped", alive: false, activeStartId: null, controlRevision: 2 } };
      return response({ stopped: true, outcome: "stopped", controlRevision: 2 });
    }
    if (path.includes("/conversations/")) return response({ conversation: { conversation_id: session!.conversationId, status: "open", agent_alive: session!.phase === "active" }, messages: [] });
    if (path.endsWith("/patches")) return response({ patches: [] });
    if (path === "/api/assistance/as-view-test") return response(session);
    throw new Error(`Unexpected assistance request: ${path}`);
  }) as typeof fetch;
  return { calls, fetchImpl };
}

let viewportWidth = 1_280;
const queries = new Map<string, MediaQueryList>();
const nativeRect = HTMLElement.prototype.getBoundingClientRect;

beforeEach(() => {
  mounted.mockClear(); unmounted.mockClear();
  queries.clear(); viewportWidth = 1_280;
  sessionStorage.clear(); localStorage.clear();
  vi.stubGlobal("crypto", webcrypto);
  vi.stubGlobal("matchMedia", vi.fn((query: string) => {
    if (!queries.has(query)) {
      const target = new EventTarget();
      queries.set(query, Object.assign(target, {
        get matches() { return query === "(max-width: 767px)" && viewportWidth <= 767; },
        media: query, onchange: null, addListener: vi.fn(), removeListener: vi.fn(),
      }) as MediaQueryList);
      Object.defineProperty(queries.get(query)!, "matches", {
        get: () => query === "(max-width: 767px)" && viewportWidth <= 767,
      });
    }
    return queries.get(query)!;
  }));
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.hasAttribute("data-separator")) return new DOMRect(viewportWidth * 0.67, 150, 11, 550);
    if (this.hasAttribute("data-group") || this.classList.contains("wb-assistance-workspace__body")) return new DOMRect(0, 150, viewportWidth, 550);
    return nativeRect.call(this);
  });
});

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function resize(width: number) {
  act(() => {
    viewportWidth = width;
    for (const query of queries.values()) query.dispatchEvent(new Event("change"));
    window.dispatchEvent(new Event("resize"));
  });
}

function mount(broker: ReturnType<typeof assistanceBroker>, provider: ViewProvider) {
  return render(
    <ThemeProvider initialPreference={{ scheme: "light", skinId: "wb.default" }}>
      <DashboardEventProvider><DashboardAnnouncer><DashboardTestRuntime>
        <AssistedDraftRuntimeProvider fetchImpl={broker.fetchImpl}>
          <CustomizeModeProvider>
            <CustomizeViewToggle />
            <ViewHost registry={registry} definition={contribution.views[0]!} provider={provider} personalizationRepository={new InMemoryPersonalizationRepository()} />
          </CustomizeModeProvider>
        </AssistedDraftRuntimeProvider>
      </DashboardTestRuntime></DashboardAnnouncer></DashboardEventProvider>
    </ThemeProvider>,
  );
}

async function launch() {
  await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
  await userEvent.click(screen.getByRole("button", { name: "AI help" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());
  await userEvent.click(screen.getByRole("button", { name: "Launch" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled());
}

describe("ViewHost assistable form keep-alive", () => {
  it("preserves a live form and conversation across the real desktop/mobile grid breakpoint", async () => {
    const broker = assistanceBroker();
    const { provider } = viewProvider();
    const rendered = mount(broker, provider);
    await launch();
    const form = screen.getByRole("region", { name: "Assisted form fields" });
    const title = screen.getByRole("textbox", { name: "Form title" });
    const composer = screen.getByRole("textbox", { name: "Message" });
    await userEvent.type(composer, "Unsent question");
    await userEvent.type(title, " retained edit");
    expect(rendered.container.querySelector(".react-grid-layout")).not.toBeNull();

    resize(600);
    await waitFor(() => expect(rendered.container.querySelector(".react-grid-layout")).toBeNull());
    expect(rendered.container.querySelector(".wb-dashboard-mobile-stack")).not.toBeNull();
    await waitFor(() => expect(screen.getByRole("radio", { name: "Form" })).toBeChecked());
    expect(screen.getByRole("region", { name: "Assisted form fields" })).toBe(form);
    expect(title).toHaveFocus();
    expect(title).toHaveValue("Original title retained edit");
    await userEvent.click(screen.getByRole("radio", { name: "AI help" }));
    expect(screen.getByRole("textbox", { name: "Message" })).toBe(composer);
    expect(composer).toHaveValue("Unsent question");

    resize(1_280);
    await waitFor(() => expect(rendered.container.querySelector(".react-grid-layout")).not.toBeNull());
    await waitFor(() => expect(screen.queryByRole("radio", { name: "AI help" })).not.toBeInTheDocument());
    expect(screen.getByRole("region", { name: "Assisted form fields" })).toBe(form);
    expect(screen.getByRole("textbox", { name: "Message" })).toBe(composer);
    expect(mounted).toHaveBeenCalledOnce();
    expect(unmounted).not.toHaveBeenCalled();
    expect(broker.calls.filter(({ path }) => path.endsWith("/start"))).toHaveLength(1);
    expect(broker.calls.filter(({ path }) => path.endsWith("/stop"))).toHaveLength(0);

    rendered.unmount();
    await waitFor(() => expect(broker.calls.filter(({ path }) => path.endsWith("/stop"))).toHaveLength(1));
  }, 15_000);

  it("retains normal Arrange/Preview authority and removal controls", async () => {
    const broker = assistanceBroker();
    const { provider } = viewProvider();
    const rendered = mount(broker, provider);
    await launch();
    await userEvent.click(screen.getByRole("button", { name: "Customize view" }));
    await waitFor(() => expect(broker.calls.filter(({ path }) => path.endsWith("/stop"))).toHaveLength(1));
    const frame = rendered.container.querySelector(".wb-widget-frame")!;
    expect(frame).toHaveAttribute("data-widget-interaction-mode", "arrange");
    expect(frame.querySelector(".wb-widget-frame__content")).toHaveAttribute("inert");
    expect(screen.getByRole("button", { name: "Preview interactions" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Actions for Assisted form" }));
    expect(screen.getByRole("menuitem", { name: "Hide" })).toBeEnabled();
    expect(screen.getByRole("menuitem", { name: "Remove" })).toBeEnabled();
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByRole("button", { name: "Preview interactions" }));
    expect(rendered.container.querySelector(".wb-widget-frame")).toHaveAttribute("data-widget-interaction-mode", "preview");
    expect(screen.getByRole("button", { name: "AI help" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "AI help" })).toBeEnabled());
    expect(broker.calls.filter(({ path }) => path.endsWith("/start"))).toHaveLength(1);
    expect(screen.queryByRole("complementary", { name: "Draft assistance" })).not.toBeInTheDocument();
  }, 15_000);

  it("revokes the previous assistant on a genuine draft-scope rebind", async () => {
    const broker = assistanceBroker();
    const fixture = viewProvider();
    mount(broker, fixture.provider);
    await launch();
    expect(screen.getByLabelText("Draft scope")).toHaveTextContent("first");
    act(() => fixture.rebind("second"));
    await waitFor(() => expect(screen.getByLabelText("Draft scope")).toHaveTextContent("second"));
    await waitFor(() => expect(broker.calls.filter(({ path }) => path.endsWith("/stop"))).toHaveLength(1));
    expect(screen.queryByRole("complementary", { name: "Draft assistance" })).not.toBeInTheDocument();
    expect(broker.calls.filter(({ path }) => path.endsWith("/start"))).toHaveLength(1);
  }, 15_000);
});
