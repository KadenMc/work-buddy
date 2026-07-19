import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  DashboardIntent,
  IntentResult,
  ReconcileResult,
  ViewSnapshot,
  WidgetSnapshot,
} from "../../../dashboard/contributions/contracts";
import { DashboardEventProvider } from "../../../dashboard/events/DashboardEventProvider";
import type { ViewProvider } from "../../../dashboard/providers/ViewProvider";
import { expectNoAccessibilityViolations } from "../../../test/setup";
import { COWORK_APP_ID, COWORK_VIEW_ID } from "../bindings";
import type { CoworkViewModel } from "../contracts";
import { InMemoryCoworkProvider } from "../providers/InMemoryCoworkProvider";
import { COWORK_VIEW_DEFINITION } from "../viewDefinition";
import { CoworkWorkspaceSurface } from "./CoworkWorkspaceSurface";

const renderSurface = () =>
  render(
    <DashboardEventProvider>
      <CoworkWorkspaceSurface
        definition={COWORK_VIEW_DEFINITION}
        provider={new InMemoryCoworkProvider()}
      />
    </DashboardEventProvider>,
  );

describe("CoworkWorkspaceSurface default (empty) mode", () => {
  const originalUrl = window.location.href;
  beforeEach(() => window.history.replaceState({}, "", "/app/cowork"));
  afterEach(() => window.history.replaceState({}, "", originalUrl));

  it("opens with honest empty states and no fabricated content", async () => {
    const { container } = renderSurface();

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
    const { container } = renderSurface();
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
    const { container } = renderSurface();
    await waitFor(
      () => expect(container.querySelector(".ProseMirror")).not.toBeNull(),
      { timeout: 10_000 },
    );
    await expectNoAccessibilityViolations(container);
  }, 15_000);
});

describe("CoworkWorkspaceSurface demo fixture (?cowork_fixture=demo)", () => {
  const originalUrl = window.location.href;
  beforeEach(() =>
    window.history.replaceState({}, "", "/app/cowork?cowork_fixture=demo"),
  );
  afterEach(() => window.history.replaceState({}, "", originalUrl));

  it("renders the fabricated demo scene behind the explicit flag", async () => {
    const { container } = renderSurface();

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

  it("keeps the scripted demo chat behind the flag", async () => {
    renderSurface();
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

/** A coarse provider that reports a live (non-demo) scope, so the surface goes live. */
class LiveCoworkProvider implements ViewProvider {
  readonly appId = COWORK_APP_ID;
  async loadView(): Promise<ViewSnapshot<CoworkViewModel>> {
    return {
      viewId: COWORK_VIEW_ID,
      revision: 1,
      observedAt: new Date(0).toISOString(),
      status: "ready",
      quality: { kind: "complete", message: "Live Co-work scope." },
      model: {
        document: {
          documentId: "live-doc",
          path: "docs/live.md",
          title: "Live doc",
          profile: "co_authored",
          driftState: "clean",
          openProposalCount: 0,
          openFlagCount: 0,
        },
      },
      bindings: {},
      widgetInputs: {},
    };
  }
  async loadWidget(): Promise<WidgetSnapshot> {
    throw new Error("single-surface view has no widgets");
  }
  async dispatch(intent: DashboardIntent): Promise<IntentResult> {
    return { intent_id: intent.intent_id, status: "accepted" };
  }
  async reconcile(): Promise<ReconcileResult> {
    return { changed: false };
  }
}

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

describe("CoworkWorkspaceSurface live mode", () => {
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
    return render(
      <DashboardEventProvider>
        <CoworkWorkspaceSurface
          definition={COWORK_VIEW_DEFINITION}
          provider={new LiveCoworkProvider()}
        />
      </DashboardEventProvider>,
    );
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
