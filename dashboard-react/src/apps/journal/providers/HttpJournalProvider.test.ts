import { afterEach, describe, expect, it, vi } from "vitest";

import { resetLocalIdentityForTests } from "../../../security/localIdentity";
import {
  JOURNAL_INSTANCE_IDS,
  JOURNAL_VIEW_DEFINITION_ID,
  JOURNAL_WIDGET_TYPE_IDS,
  toDashboardJournalIntent,
} from "../bindings";
import {
  JOURNAL_WIDGET_INSTANCE_IDS,
  type JournalCaptureSubmitIntent,
  type JournalRunningNoteEditIntent,
  type JournalRunningNoteOpenDocumentIntent,
} from "../contracts";
import {
  HttpJournalProvider,
  JOURNAL_CAPTURE_ENDPOINT,
  JOURNAL_RUNNING_NOTE_COWORK_ENDPOINT,
  JOURNAL_VIEW_ENDPOINT,
} from "./HttpJournalProvider";
import { LEGACY_TODAY_ENDPOINT } from "./LegacyFlaskViewAdapter";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const day = {
  dayId: "journal-day:2026-08-09:America/New_York:05:00",
  localDate: "2026-08-09",
  timezone: "America/New_York",
  dayBoundaryStart: "05:00",
  windowStart: "2026-08-09T05:00:00-04:00",
  windowEnd: "2026-08-10T05:00:00-04:00",
  now: "2026-08-09T21:00:00-04:00",
};

const native = {
  ok: true,
  view: {
    schemaVersion: 1,
    revision: "journal:one",
    observedAt: day.now,
    day,
    access: { mode: "read_write" },
    quality: { freshness: "current", observedAt: day.now, issues: [] },
    source: { kind: "live" },
    capture: {
      instanceId: JOURNAL_WIDGET_INSTANCE_IDS.capture,
      revision: "journal:one",
      dayId: day.dayId,
      access: { mode: "read_write" },
      smartHelp: {
        summary: "Smart uses the configured Journal model.",
        details: "Exact saved text may be sent to the configured model.",
      },
      targets: [
        {
          targetId: "running_notes",
          label: "Running Notes",
          description: "Keep it as a Running Note.",
          supportedModes: ["dumb", "smart"],
          defaultMode: "dumb",
          enabled: true,
        },
      ],
      capturesToday: 1,
      recentSubmissions: [
        {
          captureId: "capture-one",
          clientMutationId: "mutation-existing",
          targetId: "running_notes",
          mode: "dumb",
          exactText: "preserved exactly",
          submittedAt: day.now,
          persistenceStatus: "persisted",
          placementStatus: "placed",
          processingStatus: "not_requested",
          sourceRef: "wb-source://authority/item/item-one",
        },
      ],
    },
    runningNotes: {
      instanceId: JOURNAL_WIDGET_INSTANCE_IDS.runningNotes,
      revision: "journal:one",
      dayId: day.dayId,
      access: { mode: "read_only", reason: "Edit in the daily note." },
      displayMode: "chronological",
      items: [
        {
          itemId: "entry-one",
          markdown: "preserved exactly",
          createdAt: day.now,
          updatedAt: day.now,
          provenance: {
            source: "local_submission",
            label: "Submitted from the local profile; authorship not determined",
          },
          captureMode: "dumb",
          processing: { state: "not_requested" },
          resolutionState: "open",
          version: 1,
          document: {
            state: "available",
            gestureContextSha256: "a".repeat(64),
          },
        },
      ],
    },
  },
};

const legacy = {
  status: "ok",
  timezone: day.timezone,
  now: { iso: day.now, local_hhmm: "21:00", minutes_into_day: 1260 },
  work_hours: [9, 17],
  journal_day: {
    local_date: day.localDate,
    timezone: day.timezone,
    day_boundary_start: day.dayBoundaryStart,
    window_start: day.windowStart,
    window_end: day.windowEnd,
    boundary_setting_revision: "value:1",
    pending_day_boundary_start: null,
    boundary_effective_at: null,
  },
  current_contexts: [],
  recommendations: [],
  plan: [],
  focused_count: 0,
  calendar_event_count: 0,
  active_contracts: [],
  contract_constraints: [],
  engage_count: 0,
  errors: [],
};

function principal() {
  return {
    actor: {
      schema: "wb.actor-ref/v1",
      issuer_authority_id: "wia_1234567890",
      subject: "wactor_1234567890",
      kind: "human",
      tenant_scope_id: "wts_1234567890",
    },
    origin: window.location.origin,
    audience: "work-buddy-dashboard",
    session_expires_at: Date.now() / 1000 + 600,
    rotation_due_at: Date.now() / 1000 + 300,
    assurance: "enrolled_local_session",
  };
}

afterEach(() => resetLocalIdentityForTests());

describe("HttpJournalProvider", () => {
  it("binds the browser fetch receiver when no client is injected", async () => {
    const browserFetch = vi.fn(async function (
      this: typeof globalThis,
      input: RequestInfo | URL,
    ): Promise<Response> {
      if (this !== globalThis) throw new TypeError("Illegal invocation");
      const url = String(input);
      if (url === "/api/local-identity/session/csrf") {
        return json({ ok: true, authenticated: true, principal: principal(), csrf_token: "csrf" });
      }
      if (url === LEGACY_TODAY_ENDPOINT) return json(legacy);
      if (url === JOURNAL_VIEW_ENDPOINT) return json(native);
      throw new Error(`Unexpected ${url}`);
    });
    vi.stubGlobal("fetch", browserFetch);

    try {
      const provider = new HttpJournalProvider();
      const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });

      expect(snapshot.status).toBe("ready");
      expect(browserFetch.mock.contexts.length).toBeGreaterThan(0);
      expect(browserFetch.mock.contexts.every((context) => context === globalThis)).toBe(true);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("composes live capture and Running Notes with the authoritative Today timeline", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/local-identity/session/csrf") {
        return json({ ok: true, authenticated: true, principal: principal(), csrf_token: "csrf" });
      }
      if (url === LEGACY_TODAY_ENDPOINT) return json(legacy);
      if (url === JOURNAL_VIEW_ENDPOINT) return json(native);
      throw new Error(`Unexpected ${url}`);
    });
    const provider = new HttpJournalProvider({ fetchImpl });

    const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });

    expect(snapshot.status).toBe("ready");
    expect(snapshot.model?.source.kind).toBe("live");
    expect(snapshot.model?.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture].recentSubmissions[0])
      .toMatchObject({ exactText: "preserved exactly", placementStatus: "placed" });
    expect(snapshot.model?.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture].smartHelp)
      .toEqual({
        summary: "Smart uses the configured Journal model.",
        details: "Exact saved text may be sent to the configured model.",
      });
    expect(snapshot.model?.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes].access.mode)
      .toBe("read_only");
    expect(snapshot.model?.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline].accessNotice)
      .toBe("widget");
  });

  it("delegates the Timeline notice only when Journal chrome explains the same limitation", async () => {
    const reviewOnlyReason =
      "This older Today view can be reviewed here, but changes are not available.";
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/local-identity/session/csrf") {
        return json({ ok: true, authenticated: true, principal: principal(), csrf_token: "csrf" });
      }
      if (url === LEGACY_TODAY_ENDPOINT) return json(legacy);
      if (url === JOURNAL_VIEW_ENDPOINT) {
        return json({
          ...native,
          view: {
            ...native.view,
            access: { mode: "read_only", reason: reviewOnlyReason },
          },
        });
      }
      throw new Error(`Unexpected ${url}`);
    });
    const provider = new HttpJournalProvider({ fetchImpl });

    const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    const timeline = snapshot.model?.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline];

    expect(snapshot.status).toBe("read-only");
    expect(snapshot.model?.access).toEqual({ mode: "read_only", reason: reviewOnlyReason });
    expect(timeline?.access).toEqual({ mode: "read_only", reason: reviewOnlyReason });
    expect(timeline?.accessNotice).toBe("view");
  });

  it("binds a single-use gesture to the exact capture before posting", async () => {
    let gestureBody: unknown;
    let captureHeaders: Headers | undefined;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/local-identity/session/csrf") {
        return json({ ok: true, authenticated: true, principal: principal(), csrf_token: "csrf-token" });
      }
      if (url === "/api/local-identity/gestures") {
        gestureBody = JSON.parse(String(init?.body));
        return json({
          ok: true,
          gesture: {
            token: "gesture-token",
            action: "journal.capture.submit",
            subject_sha256: "s".repeat(64),
            context_sha256: (gestureBody as { context_sha256: string }).context_sha256,
            expires_at: Date.now() / 1000 + 30,
          },
        });
      }
      if (url === JOURNAL_CAPTURE_ENDPOINT) {
        captureHeaders = new Headers(init?.headers);
        return json({ ok: true, persisted: true, capture: native.view.capture.recentSubmissions[0] }, 201);
      }
      if (url === LEGACY_TODAY_ENDPOINT) return json(legacy);
      if (url === JOURNAL_VIEW_ENDPOINT) return json(native);
      throw new Error(`Unexpected ${url}`);
    });
    const provider = new HttpJournalProvider({ fetchImpl });
    await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    const intent: JournalCaptureSubmitIntent = {
      intent_type: "wb.capture.submit",
      schema_version: 1,
      intent_id: "capture-new",
      client_mutation_id: "capture-new",
      view_id: "wb.journal.main",
      instance_id: JOURNAL_WIDGET_INSTANCE_IDS.capture,
      payload: {
        day_id: day.dayId,
        target_id: "running_notes",
        mode: "dumb",
        exact_text: "  exact\ntext  ",
      },
    };

    const result = await provider.dispatch(toDashboardJournalIntent(intent));

    expect(result.status).toBe("accepted");
    expect(gestureBody).toMatchObject({
      action: "journal.capture.submit",
      subject: "journal-capture:capture-new",
    });
    expect((gestureBody as { context_sha256: string }).context_sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(captureHeaders?.get("X-WB-CSRF")).toBe("csrf-token");
    expect(captureHeaders?.get("X-WB-Gesture")).toBe("gesture-token");
  });

  it("fails closed into read-only capture when no local identity is available", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/local-identity/session/csrf") {
        return json({ ok: false, error: { code: "session_unavailable" } }, 401);
      }
      if (url === LEGACY_TODAY_ENDPOINT) return json(legacy);
      if (url === JOURNAL_VIEW_ENDPOINT) return json(native);
      throw new Error(`Unexpected ${url}`);
    });
    const provider = new HttpJournalProvider({ fetchImpl });
    const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    const capture = snapshot.model?.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture];
    expect(capture?.access.mode).toBe("read_only");
    const reason = capture?.access.mode === "read_only" ? capture.access.reason : undefined;
    expect(reason).toBe(
      "Editing is paused in this browser. Open Journal from the Work Buddy tray to reconnect.",
    );
    expect(reason).not.toMatch(/authority|authenticated|migration/i);
    expect(capture?.accessNotice).toBe("view");
    expect(snapshot.model?.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline].accessNotice)
      .toBe("widget");

    const widget = await provider.loadWidget(JOURNAL_WIDGET_TYPE_IDS.capture, {
      viewId: JOURNAL_VIEW_DEFINITION_ID,
      instanceId: JOURNAL_INSTANCE_IDS.capture,
    });
    expect(widget.status).toBe("ready");
  });

  it("uses plain guidance for unsupported inline note changes", async () => {
    const provider = new HttpJournalProvider({ fetchImpl: vi.fn() });
    const intent: JournalRunningNoteEditIntent = {
      intent_type: "wb.notes.edit-requested",
      schema_version: 1,
      intent_id: "edit-note-one",
      client_mutation_id: "edit-note-one",
      view_id: "wb.journal.main",
      instance_id: JOURNAL_WIDGET_INSTANCE_IDS.runningNotes,
      payload: { item_id: "entry-one", expected_version: 1, markdown: "updated" },
    };
    const result = await provider.dispatch(toDashboardJournalIntent(intent));

    expect(result).toMatchObject({
      status: "unavailable",
      message: "Open this note in Co-work to make changes.",
    });
  });

  it("opens a Running Note through an exact gesture without resubmitting its text", async () => {
    let gestureBody: unknown;
    let requestBody: unknown;
    const navigate = vi.fn();
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/local-identity/session/csrf") {
        return json({ ok: true, authenticated: true, principal: principal(), csrf_token: "csrf-token" });
      }
      if (url === "/api/local-identity/gestures") {
        gestureBody = JSON.parse(String(init?.body));
        return json({
          ok: true,
          gesture: {
            token: "open-gesture",
            action: "journal.running_note.open_in_cowork",
            subject_sha256: "b".repeat(64),
            context_sha256: "a".repeat(64),
            expires_at: Date.now() / 1000 + 30,
          },
        });
      }
      if (url === `${JOURNAL_RUNNING_NOTE_COWORK_ENDPOINT}/entry-one/open-in-cowork`) {
        requestBody = JSON.parse(String(init?.body));
        expect(new Headers(init?.headers).get("X-WB-Gesture")).toBe("open-gesture");
        return json({
          ok: true,
          coworkHref: "/app/cowork?store_id=store-one&document_id=doc-one",
          document: { schema: "cowork-running-note-pilot/v1" },
        }, 201);
      }
      throw new Error(`Unexpected ${url}`);
    });
    const provider = new HttpJournalProvider({ fetchImpl, navigate });
    await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" }).catch(() => undefined);
    // The action itself is independently authorized; load failures in unrelated
    // legacy Timeline data do not change its exact request contract.
    const intent: JournalRunningNoteOpenDocumentIntent = {
      intent_type: "wb.notes.open-document-requested",
      schema_version: 1,
      intent_id: "open-note-one",
      view_id: "wb.journal.main",
      instance_id: JOURNAL_WIDGET_INSTANCE_IDS.runningNotes,
      payload: {
        item_id: "entry-one",
        expected_version: 1,
        gesture_context_sha256: "a".repeat(64),
      },
    };

    const result = await provider.dispatch(toDashboardJournalIntent(intent));

    expect(result.status).toBe("accepted");
    expect(gestureBody).toMatchObject({
      action: "journal.running_note.open_in_cowork",
      subject: "journal-running-note:entry-one",
      context_sha256: "a".repeat(64),
    });
    expect(requestBody).toEqual({ expected_version: 1 });
    expect(navigate).toHaveBeenCalledWith(
      "/app/cowork?store_id=store-one&document_id=doc-one",
    );
  });
});
