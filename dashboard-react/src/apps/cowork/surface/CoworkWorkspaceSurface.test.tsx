import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  asViewId,
  asWidgetInstanceId,
  type WidgetPresentationContext,
} from "../../../dashboard/contributions/contracts";
import { DashboardEventProvider } from "../../../dashboard/events/DashboardEventProvider";
import { fallbackCanvasTheme } from "../../../theme/resolveTheme";
import { expectNoAccessibilityViolations } from "../../../test/setup";
import type { CoworkDocumentSummary, CoworkWorkspaceInput } from "../contracts";
import CoworkWorkspaceWidget from "../widget/CoworkWorkspaceWidget";
import { resolveFixtureMode } from "./CoworkWorkspaceSurface";

/**
 * The composite workspace card is a normal grid widget now, so the tests drive its renderer
 * with the hydrated WidgetRendererProps input the WidgetHost would pass, plus the URL the
 * durable exemption lets it read. The single `<main>` stands in for the grid host that owns
 * the one page landmark, mirroring how the WidgetFrame wraps the card in production.
 */
const presentation: WidgetPresentationContext = {
  instanceId: asWidgetInstanceId("wb-cowork:workspace"),
  viewId: asViewId("wb.cowork.workspace"),
  width: 1280,
  height: 720,
  sizeMode: "expanded",
  interactionMode: "operate",
  editing: false,
  theme: {
    contractVersion: 1,
    preference: { scheme: "light", skinId: "wb.default" },
    resolvedScheme: "light",
    skin: { id: "wb.default", version: 2, publisherAppId: "wb.core" },
    accessibility: {
      forcedColors: false,
      reducedMotion: false,
      reducedTransparency: false,
    },
  },
  getCanvasTheme: () => fallbackCanvasTheme("light"),
};

const noopEmit: ComponentProps<typeof CoworkWorkspaceWidget>["emit"] = async (
  intent,
) => ({ intent_id: intent.intent_id, status: "accepted" });

const DEMO_DOCUMENT: CoworkDocumentSummary = {
  documentId: "demo-doc",
  path: "docs/demo/co-work-demo.md",
  title: "Co-work demo document",
  profile: "co_authored",
  driftState: "clean",
  openProposalCount: 0,
  openFlagCount: 0,
};

const renderWorkspace = (input: CoworkWorkspaceInput) =>
  render(
    <DashboardEventProvider>
      <main>
        <CoworkWorkspaceWidget
          input={input}
          emit={noopEmit}
          presentation={presentation}
        />
      </main>
    </DashboardEventProvider>,
  );

describe("CoworkWorkspaceWidget default (empty) mode", () => {
  const originalUrl = window.location.href;
  beforeEach(() => window.history.replaceState({}, "", "/app/cowork"));
  afterEach(() => window.history.replaceState({}, "", originalUrl));

  const emptyInput: CoworkWorkspaceInput = {
    document: null,
    sessionQuality: "demo",
  };

  it("opens with honest empty states and no fabricated content", async () => {
    const { container } = renderWorkspace(emptyInput);

    // Health strip: no document open (its existing null branch).
    await waitFor(
      () =>
        expect(
          within(screen.getByLabelText("Document health")).getByText(
            "No document open",
          ),
        ).toBeVisible(),
      { timeout: 10_000 },
    );

    // The editor mounts as a real, empty editable surface, with none of the old
    // self-describing blurb and no demo document title.
    await waitFor(
      () => expect(container.querySelector(".ProseMirror")).not.toBeNull(),
      { timeout: 10_000 },
    );
    expect(screen.queryByText(/This is the editor pane/)).toBeNull();
    expect(screen.queryByText("Co-work demo document")).toBeNull();

    // Review rail: no fabricated proposals, just an honest empty layer.
    await waitFor(
      () => expect(screen.getByText("Nothing to review here.")).toBeVisible(),
      { timeout: 10_000 },
    );
    expect(
      screen.queryByText("Add the vault content hash to the cache key."),
    ).toBeNull();
  }, 15_000);

  it("shows an honest empty chat: a real composer, no scripted agent turn, no fake typing", async () => {
    const { container } = renderWorkspace(emptyInput);
    await waitFor(
      () => expect(container.querySelector(".ProseMirror")).not.toBeNull(),
      { timeout: 10_000 },
    );

    await userEvent.click(screen.getByRole("tab", { name: /Chat/ }));

    // A real composer is present.
    expect(screen.getByRole("textbox", { name: "Message" })).toBeVisible();
    // No fabricated agent message, and no perpetual typing indicator.
    expect(screen.queryByText(/I proposed a few tracked edits/)).toBeNull();
    expect(container.querySelector(".wb-chat-typing")).toBeNull();
  }, 15_000);

  it("has no accessibility violations in its empty resting state", async () => {
    const { container } = renderWorkspace(emptyInput);
    await waitFor(
      () => expect(container.querySelector(".ProseMirror")).not.toBeNull(),
      { timeout: 10_000 },
    );
    await expectNoAccessibilityViolations(container);
  }, 15_000);
});

// The demo scene is no longer a product surface (Ruling 1): it is a dev-only fixture entry the
// e2e suites drive against the dev server, gated by import.meta.env.DEV so production tree-shakes
// it. The unit environment runs with DEV true, so ?cowork_fixture=demo still composes the scene
// here exactly as it does for the dev server. The production gate is proven in resolveFixtureMode
// below.
describe("CoworkWorkspaceWidget dev-only demo fixture entry (?cowork_fixture=demo)", () => {
  const originalUrl = window.location.href;
  beforeEach(() =>
    window.history.replaceState({}, "", "/app/cowork?cowork_fixture=demo"),
  );
  afterEach(() => window.history.replaceState({}, "", originalUrl));

  const demoInput: CoworkWorkspaceInput = {
    document: DEMO_DOCUMENT,
    sessionQuality: "demo",
  };

  it("composes the fixture scene behind the dev-only entry", async () => {
    const { container } = renderWorkspace(demoInput);

    // Health strip reflects the demo document session.
    await waitFor(
      () => expect(screen.getByText("Co-work demo document")).toBeVisible(),
      { timeout: 10_000 },
    );
    expect(screen.getByText("In sync")).toBeVisible();
    expect(screen.getByText("0 open proposals")).toBeVisible();

    // The demo review rail fixture is present.
    expect(
      screen.getByText("Add the vault content hash to the cache key."),
    ).toBeVisible();

    // The demo editor seeds coherent prose (scoped to the editor, since the rail also
    // quotes these phrases), not the self-describing blurb.
    await waitFor(
      () => expect(container.querySelector(".ProseMirror")).not.toBeNull(),
      { timeout: 10_000 },
    );
    expect(
      within(screen.getByLabelText("Editor")).getByText(/Context bundle cache/),
    ).toBeVisible();
    expect(screen.queryByText(/This is the editor pane/)).toBeNull();
  }, 15_000);

  it("keeps the scripted demo chat behind the dev-only entry", async () => {
    renderWorkspace(demoInput);
    await waitFor(
      () => expect(screen.getByText("Co-work demo document")).toBeVisible(),
      { timeout: 10_000 },
    );

    await userEvent.click(screen.getByRole("tab", { name: /Chat/ }));
    expect(screen.getByRole("tab", { name: /Chat/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await waitFor(
      () =>
        expect(screen.getByText(/I proposed a few tracked edits/)).toBeVisible(),
      { timeout: 10_000 },
    );
  }, 15_000);
});

const LIVE_DOCUMENT: CoworkDocumentSummary = {
  documentId: "live-doc",
  path: "docs/live.md",
  title: "Live doc",
  profile: "co_authored",
  driftState: "clean",
  openProposalCount: 0,
  openFlagCount: 0,
};

/** The R2 doc-open payload the stubbed route returns, one edit proposal on the seed text. */
const R2_LIVE_PAYLOAD = {
  document_id: "live-doc",
  store_id: "live-store",
  path: "docs/live.md",
  title: "live.md",
  profile: "co_authored",
  hashes: {
    ydoc_snapshot_sha256: null,
    last_materialized_sha256: null,
    current_file_sha256: "filesha",
  },
  drift: { state: "clean", diff_available: false },
  open_proposals: [
    {
      proposal_id: "s1",
      kind: "edit",
      quote_anchor: { exact: "editor pane", prefix: "This is the ", suffix: "." },
      replacement: "editor pane and its review rail",
      rationale: "Name the rail the pane pairs with.",
      tldr: "Name the review rail.",
      producer: {
        model: "research-agent",
        model_source: "session-manifest",
        session_id: "sess-1",
        surface: "mcp",
      },
      epistemic_state: "ai_proposed",
      base_doc_sha256: "base",
      canonical_sha256: "canon-s1",
      base_ok: true,
      status: "open",
      fixes_ref: null,
      claim_refs: [],
      created_at: "2026-07-17T12:00:00Z",
    },
  ],
  expressions: [],
  provenance_spans: [],
  events_cursor: "c0",
};

const jsonResponse = (body: unknown, status = 200): Response =>
  ({
    ok: status < 400,
    status,
    headers: { get: () => null },
    json: async () => body,
    arrayBuffer: async () => new ArrayBuffer(0),
  }) as unknown as Response;

const emptyYdocResponse = (): Response =>
  ({
    ok: true,
    status: 200,
    headers: {
      get: (name: string) =>
        name === "X-WB-Next-Offset" ? "0" : name === "X-WB-Doc-Sha256" ? "h0" : null,
    },
    arrayBuffer: async () => new ArrayBuffer(0),
    json: async () => ({}),
  }) as unknown as Response;

/** Route the live surface's direct route calls: R2 read, R3 ydoc pull, R4 ydoc push. */
const liveFetch = () =>
  vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (url.includes("/ydoc")) {
      if (method === "POST") {
        return jsonResponse({ ok: true, applied: true, doc_sha256: "h1", next_offset: "1" });
      }
      return emptyYdocResponse();
    }
    if (url.includes("/api/truth/doc/live-doc")) {
      return jsonResponse(R2_LIVE_PAYLOAD);
    }
    return jsonResponse({ error: "not_found" }, 404);
  });

describe("CoworkWorkspaceWidget live mode", () => {
  const originalFetch = globalThis.fetch;
  const originalUrl = window.location.href;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    window.history.replaceState({}, "", originalUrl);
    vi.restoreAllMocks();
  });

  const renderLive = () => {
    window.history.replaceState({}, "", "/app/cowork?store_id=live-store");
    globalThis.fetch = liveFetch() as unknown as typeof fetch;
    return renderWorkspace({ document: LIVE_DOCUMENT, sessionQuality: "complete" });
  };

  it("pulls R2 and ingests the proposal so a card and a suggestion mark both render", async () => {
    const { container } = renderLive();

    // The live pull drives the rail card from the one source of truth.
    await waitFor(() => expect(screen.getByText("Name the review rail.")).toBeVisible(), {
      timeout: 10_000,
    });

    // The SAME pull ingests the proposal into the editor, so a suggestion mark renders.
    await waitFor(
      () => expect(container.querySelector("[data-wb-suggestion]")).not.toBeNull(),
      { timeout: 10_000 },
    );

    // The health strip reflects the live pull's open-proposal count.
    expect(screen.getByText("1 open proposal")).toBeVisible();
  }, 15_000);
});

describe("resolveFixtureMode: demo is dev-only, empty and live are the product modes", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("resolves ?cowork_fixture=demo to the demo scene when DEV is true", () => {
    vi.stubEnv("DEV", true);
    expect(resolveFixtureMode("demo", "demo-doc", undefined, "demo")).toBe("demo");
  });

  it("falls back to the honest empty default for ?cowork_fixture=demo in a production build", () => {
    // import.meta.env.DEV is statically false in production, so the demo entry is scrapped and
    // the CoworkDemoWorkspace it would select is tree-shaken out.
    vi.stubEnv("DEV", false);
    expect(resolveFixtureMode("demo", "demo-doc", undefined, "demo")).toBe("empty");
  });

  it("resolves a store-scoped session to live regardless of the demo gate", () => {
    vi.stubEnv("DEV", false);
    expect(resolveFixtureMode("complete", "live-doc", "live-store", null)).toBe("live");
  });

  it("defaults to the honest empty state with no override and no store id", () => {
    expect(resolveFixtureMode("demo", undefined, undefined, null)).toBe("empty");
  });
});
