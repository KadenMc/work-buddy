import { afterEach, describe, expect, it, vi } from "vitest";
import { createHash } from "node:crypto";

import type { DashboardIntent } from "../../../dashboard/contributions/contracts";
import { asWidgetInstanceId } from "../../../dashboard/contributions/contracts";
import { assertDashboardIntent } from "../../../dashboard/providers/validateProviderBoundary";
import { resetLocalIdentityForTests } from "../../../security/localIdentity";
import {
  JOURNAL_INSTANCE_IDS,
  JOURNAL_GENERIC_WIDGET_TYPE_ID,
  JOURNAL_VIEW_DEFINITION_ID,
  JOURNAL_WIDGET_TYPE_IDS,
  toDashboardJournalIntent,
} from "../bindings";
import {
  JOURNAL_WIDGET_INSTANCE_IDS,
  type JournalCaptureSubmitIntent,
  type JournalFieldValuePutIntent,
  type JournalItemActionIntent,
  type JournalPromptGenerateIntent,
  type JournalRunningNoteEditIntent,
  type JournalRunningNoteOpenDocumentIntent,
} from "../contracts";
import {
  HttpJournalProvider,
  JOURNAL_CAPTURE_ENDPOINT,
  JOURNAL_DOCUMENT_MODULE_ENDPOINT,
  JOURNAL_FIELD_VALUES_ENDPOINT,
  JOURNAL_ITEMS_ENDPOINT,
  JOURNAL_PROMPT_INTERACTIONS_ENDPOINT,
  JOURNAL_RUNNING_NOTE_COWORK_ENDPOINT,
  JOURNAL_VIEW_ENDPOINT,
  journalCaptureGestureContext,
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
  const proposalFollowUp = { kind: "app_link", referenceId: "th-0123abcd", label: "Review in Tasks",
    description: "Task proposal ready — no task has been created.", href: "/app/tasks?proposal=th-0123abcd" };
  const disclosed = { state: "ready", code: "ready", reason: "Smart is ready.",
    disclosure: { provider: "fixture", model: "deterministic-test", maxInputBytes: 32768, tools: false, web: false } };
  function proposalFixture(href = proposalFollowUp.href) {
    return { ...native, view: { ...native.view, capture: { ...native.view.capture,
      smartAvailability: disclosed,
      recentSubmissions: [{ ...native.view.capture.recentSubmissions[0], followUps: [{ ...proposalFollowUp, href }] }],
    } } };
  }
  function fixtureFetch(value: unknown) {
    return vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/local-identity/session/csrf") return json({ ok: true, authenticated: true, principal: principal(), csrf_token: "csrf" });
      if (String(input) === LEGACY_TODAY_ENDPOINT) return json(legacy);
      if (String(input) === JOURNAL_VIEW_ENDPOINT) return json(value);
      throw new Error(`Unexpected ${String(input)}`);
    });
  }

  it("uses the immutable native composition without consulting the legacy timeline", async () => {
    const modules = [
      ["capture", "simple.capture", "Capture"],
      ["day_stream", "simple.stream", "Day stream"],
      ["record_collection", "simple.notes", "Notes"],
      ["field_group", "custom.check-in", "Check-in"],
    ].map(([moduleTypeId, moduleInstanceId, label], ordinal) => ({
      slotId: `slot.${moduleInstanceId}`,
      ordinal,
      moduleInstanceId,
      moduleInstanceVersion: 1,
      moduleTypeId,
      moduleTypeVersion: 1,
      label,
      semanticMembership: "included",
      settings: {},
      scheduleKind: "always",
      scheduleEvidence: null,
      fields: moduleTypeId === "field_group"
        ? [{
            compositionSlotId: "check-in.focus", ordinal: 0,
            fieldId: "field.focus", fieldDefinitionVersion: 1,
            label: "Focus", description: "How is your focus?", valueKind: "scale",
            unit: "1–5", constraints: { minimum: 1, maximum: 5 },
            functionId: "function.focus", functionVersion: 1,
            behaviorId: "behavior.focus", behaviorVersion: 1,
            privacyClass: "private", searchMode: "exact",
            promptId: null, promptVersion: null, promptWording: null,
            promptHelp: null, promptRequiredness: "optional",
          }]
        : [],
    }));
    const fixture = {
      ...native,
      view: {
        ...native.view,
        effectiveComposition: {
          schemaVersion: 1,
          persisted: true,
          snapshotId: "day-composition-1",
          snapshotVersion: 1,
          compositionDigest: "composition-digest",
          searchRecipeVersion: 1,
          activationRevision: 1,
          authorityState: "database_only",
          profile: {
            profileId: "simple",
            profileRevision: 1,
            formatVersion: 1,
            name: "Simple Journal",
            description: "A useful starting point.",
            profileDigest: "profile-digest",
          },
          modules,
        },
        logEntries: [{
          itemId: "log-1", itemKind: "record", markdown: "Native log entry",
          text: "Native log entry", createdAt: day.now, updatedAt: day.now,
          revision: 1, lifecycle: "active", authorityKind: "human",
          sourceRef: "wb-source://journal/log-1", moduleInstanceId: "simple.stream",
          moduleInstanceVersion: 1,
        }],
        fieldValues: [{
          valueId: "value-1", localDate: day.localDate,
          moduleInstanceId: "custom.check-in", moduleInstanceVersion: 1,
          fieldId: "field.focus", fieldDefinitionVersion: 1,
          valueKind: "scale", disposition: null, value: 4, currentRevision: 1,
          authorship: "human", reviewState: "reviewed",
          sourceRef: "wb-source://journal/value-1", observedAt: day.now,
          statedAt: day.now, ingestedAt: day.now, lifecycle: "active",
        }],
        nativeItems: [
          {
            itemId: "generated-1", itemKind: "generated_artifact",
            text: "Generated briefing", createdAt: day.now, updatedAt: day.now,
            revision: 1, lifecycle: "current", authorityKind: "generated",
            sourceRef: "wb-source://journal/generated-1",
            moduleInstanceId: "custom.check-in", moduleInstanceVersion: 1,
          },
          {
            itemId: "stream-brief", itemKind: "generated_artifact",
            text: "Day briefing", createdAt: day.now, updatedAt: day.now,
            revision: 1, lifecycle: "current", authorityKind: "generated",
            moduleInstanceId: "simple.stream", moduleInstanceVersion: 1,
          },
        ],
      },
    };
    const fetchImpl = fixtureFetch(fixture);
    const provider = new HttpJournalProvider({ fetchImpl });

    const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });

    expect(fetchImpl.mock.calls.map(([input]) => String(input))).not.toContain(LEGACY_TODAY_ENDPOINT);
    expect(snapshot.effectiveComposition?.defaultSlots.map((slot) => slot.defaultInstanceId)).toEqual([
      "simple.capture", "simple.stream", "simple.notes", "custom.check-in",
    ]);
    expect(snapshot.widgetInputs["simple.stream"]).toMatchObject({
      items: [
        { itemId: "log-1", title: "Native log entry" },
        { itemId: "stream-brief", title: "Day briefing" },
      ],
    });
    const generic = await provider.loadWidget(JOURNAL_GENERIC_WIDGET_TYPE_ID, {
      viewId: JOURNAL_VIEW_DEFINITION_ID,
      instanceId: asWidgetInstanceId("custom.check-in"),
      bindings: {},
    });
    expect(generic.input).toMatchObject({
      label: "Check-in",
      localDate: day.localDate,
      moduleInstanceVersion: 1,
      fields: [{
        valueId: "value-1",
        compositionSlotId: "check-in.focus",
        fieldId: "field.focus",
        functionId: "function.focus",
        functionVersion: 1,
        label: "Focus",
        value: 4,
        readOnly: false,
      }],
      items: [{ itemId: "generated-1", itemKind: "generated_artifact", text: "Generated briefing" }],
    });
  });

  it("opens a configured document module through an exact gesture and returns its panel target", async () => {
    const documentModule = {
      slotId: "reflection", ordinal: 0,
      moduleInstanceId: "custom.reflection", moduleInstanceVersion: 2,
      moduleTypeId: "document", moduleTypeVersion: 1, label: "Daily reflection",
      behaviorId: "provenance_only", behaviorVersion: 1, aiContribution: "allowed",
      semanticMembership: "included", settings: {
        documentRole: "daily_reflection", truthEligibility: "allowed",
        initialTruthActivation: "disabled",
      }, scheduleKind: "always", scheduleEvidence: null, fields: [],
      document: {
        state: "available", role: "daily_reflection", truthEligibility: "allowed",
        truthStartsDisabled: true,
      },
    };
    const fixture = {
      ...native,
      view: {
        ...native.view,
        effectiveComposition: {
          schemaVersion: 1, persisted: true, snapshotId: "snapshot-document",
          snapshotVersion: 1, compositionDigest: "document-composition",
          searchRecipeVersion: 1, activationRevision: 1,
          authorityState: "database_only",
          profile: {
            profileId: "custom", profileRevision: 1, formatVersion: 1,
            name: "Custom", description: "", profileDigest: "profile-document",
          },
          modules: [documentModule],
        },
        logEntries: [], fieldValues: [], nativeItems: [], promptInteractions: [],
      },
    };
    let gestureBody: Record<string, unknown> | undefined;
    let openBody: Record<string, unknown> | undefined;
    let openHeaders: Headers | undefined;
    const target = {
      state: "current", role: "daily_reflection", truthEligibility: "allowed",
      truthStartsDisabled: true,
      href: "/app/cowork?store_id=store-one&document_id=doc-one",
      storeId: "store-one", documentId: "doc-one", bindingId: "binding-one",
      domainEntityId: "entity-one", contentAuthorityEpoch: 1, canOpenFull: true,
    };
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/local-identity/session/csrf") {
        return json({ ok: true, authenticated: true, principal: principal(), csrf_token: "csrf" });
      }
      if (url === JOURNAL_VIEW_ENDPOINT) return json(fixture);
      if (url === "/api/local-identity/gestures") {
        gestureBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return json({ ok: true, gesture: {
          token: "document-gesture", action: "journal.document.open",
          subject_sha256: "a".repeat(64),
          context_sha256: gestureBody.context_sha256,
          expires_at: Date.now() / 1000 + 30,
        } });
      }
      if (url === `${JOURNAL_DOCUMENT_MODULE_ENDPOINT}/${day.localDate}/custom.reflection/open`) {
        openBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
        openHeaders = new Headers(init?.headers);
        return json({ ok: true, deduplicated: false, document: target }, 201);
      }
      throw new Error(`Unexpected ${url}`);
    });
    const provider = new HttpJournalProvider({ fetchImpl });
    const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    expect(snapshot.widgetInputs["custom.reflection"]).toMatchObject({
      moduleTypeId: "document",
      document: { state: "available", role: "daily_reflection" },
    });

    const result = await provider.dispatch({
      intent_type: "wb.journal.document.open",
      schema_version: 1,
      intent_id: "open-document-module-one",
      client_mutation_id: "journal-document-open-one",
      view_id: JOURNAL_VIEW_DEFINITION_ID,
      instance_id: asWidgetInstanceId("custom.reflection"),
      payload: { local_date: day.localDate, module_instance_version: 2 },
    });

    expect(result).toMatchObject({ status: "accepted", value: target });
    expect(gestureBody).toMatchObject({
      action: "journal.document.open",
      subject: `journal-document:${day.localDate}:custom.reflection`,
      context_sha256: expect.stringMatching(/^[0-9a-f]{64}$/u),
    });
    expect(openBody).toEqual({
      clientMutationId: "journal-document-open-one",
      moduleInstanceVersion: 2,
    });
    expect(openHeaders?.get("X-WB-Gesture")).toBe("document-gesture");
  });

  it("posts typed field edits through a separate exact gesture and CAS envelope", async () => {
    const fieldModule = {
      slotId: "check-in", ordinal: 0,
      moduleInstanceId: "custom.check-in", moduleInstanceVersion: 3,
      moduleTypeId: "field_group", moduleTypeVersion: 1, label: "Check-in",
      semanticMembership: "included", settings: {}, scheduleKind: "always",
      scheduleEvidence: null,
      fields: [{
        compositionSlotId: "check-in:focus", ordinal: 0,
        fieldId: "field.focus", fieldDefinitionVersion: 2,
        label: "Focus", description: "Usable focus", valueKind: "scale",
        unit: null, constraints: { minimum: 1, maximum: 5 },
        behaviorId: "human_value", behaviorVersion: 1,
        privacyClass: "private", searchMode: "structured_only",
        promptId: "prompt.focus", promptVersion: 1,
        promptWording: "How is your focus?", promptHelp: null,
        promptRequiredness: "optional",
      }],
    };
    const fixture = {
      ...native,
      view: {
        ...native.view,
        effectiveComposition: {
          schemaVersion: 1, persisted: true, snapshotId: "snapshot-field",
          snapshotVersion: 1, compositionDigest: "field-composition",
          searchRecipeVersion: 1, activationRevision: 1,
          authorityState: "database_only",
          profile: {
            profileId: "custom", profileRevision: 1, formatVersion: 1,
            name: "Custom", description: "", profileDigest: "profile-field",
          },
          modules: [fieldModule],
        },
        logEntries: [], fieldValues: [], nativeItems: [],
      },
    };
    let gestureBody: Record<string, unknown> | undefined;
    let requestBody: Record<string, unknown> | undefined;
    let requestHeaders: Headers | undefined;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/local-identity/session/csrf") {
        return json({ ok: true, authenticated: true, principal: principal(), csrf_token: "csrf-token" });
      }
      if (url === JOURNAL_VIEW_ENDPOINT) return json(fixture);
      if (url === "/api/local-identity/gestures") {
        gestureBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return json({ ok: true, gesture: {
          token: "field-gesture", action: "journal.field_value.put",
          subject_sha256: "b".repeat(64),
          context_sha256: gestureBody.context_sha256,
          expires_at: Date.now() / 1000 + 30,
        } });
      }
      if (url === JOURNAL_FIELD_VALUES_ENDPOINT) {
        requestBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
        requestHeaders = new Headers(init?.headers);
        return json({ ok: true, deduplicated: false, fieldValue: {
          valueId: "jfv-focus", localDate: day.localDate,
          moduleInstanceId: "custom.check-in", moduleInstanceVersion: 3,
          fieldId: "field.focus", fieldDefinitionVersion: 2,
          valueKind: "scale", disposition: null, value: 4,
          currentRevision: 1, authorship: "human", reviewState: "not_applicable",
          sourceRef: "wb-source://journal/focus", lifecycle: "current",
        } });
      }
      throw new Error(`Unexpected ${url}`);
    });
    const provider = new HttpJournalProvider({ fetchImpl });
    await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    const intent: JournalFieldValuePutIntent = {
      intent_type: "wb.journal.field-value.put",
      schema_version: 1,
      intent_id: "field-edit-one",
      client_mutation_id: "journal-field-mutation-one",
      view_id: "wb.journal.main",
      instance_id: "custom.check-in",
      payload: {
        local_date: day.localDate,
        module_instance_id: "custom.check-in",
        module_instance_version: 3,
        composition_slot_id: "check-in:focus",
        field_id: "field.focus",
        field_definition_version: 2,
        expected_revision: 0,
        value: 4,
        exact_input: "4",
        stated_at: day.now,
      },
    };

    const result = await provider.dispatch(toDashboardJournalIntent(intent));

    expect(result).toMatchObject({ status: "accepted", message: "Journal field saved." });
    expect(gestureBody).toMatchObject({
      action: "journal.field_value.put",
      subject: `journal-field:${day.localDate}:custom.check-in:field.focus`,
      context_sha256: expect.stringMatching(/^[0-9a-f]{64}$/u),
    });
    expect(requestHeaders?.get("X-WB-Gesture")).toBe("field-gesture");
    expect(requestBody).toEqual({
      clientMutationId: "journal-field-mutation-one",
      localDate: day.localDate,
      moduleInstanceId: "custom.check-in",
      moduleInstanceVersion: 3,
      compositionSlotId: "check-in:focus",
      fieldId: "field.focus",
      fieldDefinitionVersion: 2,
      expectedRevision: 0,
      value: 4,
      exactInput: "4",
      statedAt: day.now,
    });
  });

  it("dispatches native item CAS actions and surfaces retryable generation failure", async () => {
    const promptModule = {
      slotId: "context", ordinal: 0,
      moduleInstanceId: "custom.context", moduleInstanceVersion: 1,
      moduleTypeId: "prompt_result", moduleTypeVersion: 1, label: "Context",
      semanticMembership: "included", settings: {}, scheduleKind: "always",
      scheduleEvidence: null,
      fields: [{
        compositionSlotId: "context:topic", ordinal: 0,
        fieldId: "field.topic", fieldDefinitionVersion: 1,
        label: "Topic", valueKind: "long_text", constraints: {},
        promptId: "prompt.topic", promptVersion: 1,
        promptWording: "Add useful context", promptHelp: null,
        promptRequiredness: "optional",
      }],
    };
    const fixture = {
      ...native,
      view: {
        ...native.view,
        runningNotes: { ...native.view.runningNotes, access: { mode: "read_write" } },
        effectiveComposition: {
          schemaVersion: 1, persisted: true, snapshotId: "snapshot-context",
          snapshotVersion: 1, compositionDigest: "context-composition",
          searchRecipeVersion: 1, activationRevision: 1,
          authorityState: "database_only",
          profile: {
            profileId: "custom", profileRevision: 1, formatVersion: 1,
            name: "Custom", description: "", profileDigest: "profile-context",
          },
          modules: [promptModule],
        },
        logEntries: [], fieldValues: [],
        nativeItems: [{
          itemId: "jni_context", itemKind: "record", text: "Current context",
          createdAt: day.now, updatedAt: day.now, revision: 2,
          lifecycle: "current", authorityKind: "native_plain",
          actions: ["edit", "correct", "resolve", "route", "tombstone"],
          relations: [],
          moduleInstanceId: "custom.context", moduleInstanceVersion: 1,
        }],
        promptInteractions: [{
          interactionId: "jpi_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          moduleInstanceId: "custom.context", moduleInstanceVersion: 1,
          promptId: "prompt.topic", promptVersion: 1,
          promptWording: "Add useful context", promptHelp: null,
          inputText: "A short topic seed.", lifecycle: "current", currentRevision: 1,
          variants: [], generationRequests: [],
        }],
      },
    };
    const itemBodies: unknown[] = [];
    const generationBodies: unknown[] = [];
    let generationReplay = false;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/local-identity/session/csrf") {
        return json({ ok: true, authenticated: true, principal: principal(), csrf_token: "csrf-token" });
      }
      if (url === JOURNAL_VIEW_ENDPOINT) return json(fixture);
      if (url === "/api/local-identity/gestures") {
        const gesture = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return json({ ok: true, gesture: {
          token: "journal-action-gesture", action: gesture.action,
          subject_sha256: "b".repeat(64), context_sha256: gesture.context_sha256,
          expires_at: Date.now() / 1000 + 30,
        } });
      }
      if (url === `${JOURNAL_ITEMS_ENDPOINT}/jni_context/resolve`) {
        itemBodies.push(JSON.parse(String(init?.body)));
        return json({ ok: true, item: {
          ...fixture.view.nativeItems[0], revision: 3, lifecycle: "resolved",
          actions: ["route", "restore", "tombstone"],
        } });
      }
      if (url === `${JOURNAL_PROMPT_INTERACTIONS_ENDPOINT}/jpi_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/generate`) {
        generationBodies.push(JSON.parse(String(init?.body)));
        if (generationReplay) {
          return json({
            ok: true,
            message: "A previous launch failed. Choose Generate again to retry.",
            generation: {
              requestId: "jpgr_cccccccccccccccccccccccccccccccc",
              status: "failed", retryable: true, attempts: 1,
              errorCode: "generation_worker_start_failed",
              createdAt: day.now, updatedAt: day.now, completedAt: day.now,
            },
          });
        }
        return json({ ok: false, error: {
          code: "journal_prompt_generation_start_failed",
          message: "The background agent could not start. Choose Generate again to retry.",
          retryable: true,
        } }, 503);
      }
      throw new Error(`Unexpected ${url}`);
    });
    const provider = new HttpJournalProvider({ fetchImpl });
    await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    const itemIntent: JournalItemActionIntent = {
      intent_type: "wb.journal.item-action", schema_version: 1,
      intent_id: "resolve-context", client_mutation_id: "resolve-context-mutation",
      view_id: "wb.journal.main", instance_id: "custom.context",
      payload: { item_id: "jni_context", action: "resolve", expected_revision: 2 },
    };
    expect(await provider.dispatch(toDashboardJournalIntent(itemIntent))).toMatchObject({
      status: "accepted",
    });
    expect(itemBodies).toEqual([{
      clientMutationId: "resolve-context-mutation", expectedRevision: 2,
    }]);

    await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "refresh" });
    const generationIntent: JournalPromptGenerateIntent = {
      intent_type: "wb.journal.prompt-generate", schema_version: 1,
      intent_id: "generate-context", client_mutation_id: "generate-context-mutation",
      view_id: "wb.journal.main", instance_id: "custom.context",
      payload: {
        interaction_id: "jpi_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        expected_revision: 1,
      },
    };
    expect(await provider.dispatch(toDashboardJournalIntent(generationIntent))).toMatchObject({
      status: "unavailable",
      message: "The background agent could not start. Choose Generate again to retry.",
    });
    expect(generationBodies).toEqual([{
      clientMutationId: "generate-context-mutation", expectedRevision: 1,
    }]);

    generationReplay = true;
    await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "refresh" });
    expect(await provider.dispatch(toDashboardJournalIntent(generationIntent))).toMatchObject({
      status: "unavailable",
      message: "A previous launch failed. Choose Generate again to retry.",
    });
  });

  it("polls with backoff only while a prompt generation is active", async () => {
    vi.useFakeTimers();
    try {
      let reads = 0;
      const prompt = {
        interactionId: "jpi_dddddddddddddddddddddddddddddddd",
        moduleInstanceId: "custom.context", moduleInstanceVersion: 1,
        promptId: "prompt.topic", promptVersion: 1,
        promptWording: "Add useful context", promptHelp: null,
        inputText: "A short topic seed.", lifecycle: "current", currentRevision: 1,
        variants: [],
      };
      const composition = {
        schemaVersion: 1, persisted: true, snapshotId: "snapshot-poll",
        snapshotVersion: 1, compositionDigest: "poll-composition",
        searchRecipeVersion: 1, activationRevision: 1, authorityState: "database_only",
        profile: {
          profileId: "custom", profileRevision: 1, formatVersion: 1,
          name: "Custom", description: "", profileDigest: "profile-poll",
        },
        modules: [],
      };
      const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/local-identity/session/csrf") {
          return json({ ok: true, authenticated: true, principal: principal(), csrf_token: "csrf-token" });
        }
        if (url !== JOURNAL_VIEW_ENDPOINT) throw new Error(`Unexpected ${url}`);
        reads += 1;
        const active = reads === 1;
        return json({ ...native, view: {
          ...native.view,
          revision: active ? "journal:poll-one" : "journal:poll-two",
          effectiveComposition: composition,
          promptInteractions: [{ ...prompt, generationRequests: [{
            requestId: "jpgr_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            status: active ? "leased" : "succeeded",
            retryable: false, attempts: 1,
            createdAt: day.now, updatedAt: day.now,
            ...(active ? {} : { completedAt: day.now }),
          }] }],
        } });
      });
      const provider = new HttpJournalProvider({ fetchImpl, clock: () => day.now });
      let resolveReconcile!: () => void;
      const reconciled = new Promise<void>((resolve) => { resolveReconcile = resolve; });
      const unsubscribe = provider.subscribeInvalidations((invalidation) => {
        void provider.reconcile(invalidation).then(() => resolveReconcile());
      });
      await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });

      await vi.advanceTimersByTimeAsync(1_000);
      await reconciled;
      expect(reads).toBe(2);
      await vi.advanceTimersByTimeAsync(60_000);
      expect(reads).toBe(2);
      unsubscribe();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps recovery-fenced database content read-only without consulting legacy", async () => {
    const fixture = {
      ...native,
      view: {
        ...native.view,
        effectiveComposition: {
          schemaVersion: 1,
          persisted: false,
          snapshotId: null,
          snapshotVersion: null,
          compositionDigest: "recovery-composition",
          searchRecipeVersion: 1,
          activationRevision: 1,
          authorityState: "recovery_fenced",
          profile: {
            profileId: "simple-journal", profileRevision: 1, formatVersion: 1,
            name: "Simple Journal", description: "Simple", profileDigest: "profile",
          },
          modules: [],
        },
      },
    };
    const fetchImpl = fixtureFetch(fixture);
    const provider = new HttpJournalProvider({ fetchImpl });

    const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });

    expect(fetchImpl.mock.calls.map(([input]) => String(input))).not.toContain(LEGACY_TODAY_ENDPOINT);
    expect(snapshot.status).toBe("read-only");
    expect(snapshot.model?.access).toEqual({
      mode: "read_only",
      reason: "Journal recovery is still reconciling. Editing is paused.",
    });
    expect(snapshot.model?.quality.issues).toContainEqual(expect.objectContaining({
      code: "journal_recovery_fenced",
    }));
  });

  it("scopes repeated record collections to their owning module", async () => {
    const modules = ["notes.one", "notes.two"].map((moduleInstanceId, ordinal) => ({
      slotId: `slot.${moduleInstanceId}`,
      ordinal,
      moduleInstanceId,
      moduleInstanceVersion: 1,
      moduleTypeId: "record_collection",
      moduleTypeVersion: 1,
      label: moduleInstanceId,
      semanticMembership: "included",
      settings: {},
      scheduleKind: "always",
      scheduleEvidence: null,
      fields: [],
    }));
    const fixture = {
      ...native,
      view: {
        ...native.view,
        effectiveComposition: {
          schemaVersion: 1, persisted: false, snapshotId: null, snapshotVersion: null,
          compositionDigest: "collections", searchRecipeVersion: 1, activationRevision: 1,
          authorityState: "database_only",
          profile: { profileId: "collections", profileRevision: 1, formatVersion: 1,
            name: "Collections", description: "", profileDigest: "collections-profile" },
          modules,
        },
        runningNotes: {
          ...native.view.runningNotes,
          items: [
            { ...native.view.runningNotes.items[0], itemId: "one", moduleInstanceId: "notes.one", moduleInstanceVersion: 1 },
            { ...native.view.runningNotes.items[0], itemId: "two", moduleInstanceId: "notes.two", moduleInstanceVersion: 1 },
          ],
        },
        nativeItems: [{
          itemId: "artifact-two", itemKind: "generated_artifact",
          text: "Collection two briefing", createdAt: day.now, updatedAt: day.now,
          revision: 1, lifecycle: "current", authorityKind: "generated",
          moduleInstanceId: "notes.two", moduleInstanceVersion: 1,
        }],
      },
    };
    const provider = new HttpJournalProvider({ fetchImpl: fixtureFetch(fixture) });
    await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });

    const first = await provider.loadWidget(JOURNAL_WIDGET_TYPE_IDS.runningNotes, {
      viewId: JOURNAL_VIEW_DEFINITION_ID,
      instanceId: asWidgetInstanceId("notes.one"),
      bindings: {},
    });
    const second = await provider.loadWidget(JOURNAL_WIDGET_TYPE_IDS.runningNotes, {
      viewId: JOURNAL_VIEW_DEFINITION_ID,
      instanceId: asWidgetInstanceId("notes.two"),
      bindings: {},
    });
    expect(first.input).toMatchObject({ items: [{ itemId: "one" }] });
    expect(first.input).not.toHaveProperty("supplementalItems");
    expect(second.input).toMatchObject({
      items: [{ itemId: "two" }],
      supplementalItems: [{
        itemId: "artifact-two", itemKind: "generated_artifact",
        text: "Collection two briefing",
      }],
    });
  });

  const smartCaptureId = "a".repeat(32);
  function smartFixture(model = "reviewed-model", provider = "reviewed-provider") {
    const fixture = proposalFixture();
    return { ...fixture, view: { ...fixture.view, capture: { ...fixture.view.capture,
      smartAvailability: { ...disclosed, disclosure: { ...disclosed.disclosure, model, provider } },
      recentSubmissions: [{ ...fixture.view.capture.recentSubmissions[0],
        captureId: smartCaptureId, revision: 4, mode: "smart" }],
    } } };
  }
  function smartIntent(operation: "submit" | "retry", disclosureSha256?: string): DashboardIntent {
    const disclosure = disclosureSha256 === undefined ? {} : { smart_disclosure_sha256: disclosureSha256 };
    const submit = toDashboardJournalIntent({
      intent_type: "wb.capture.submit", schema_version: 1, intent_id: "reviewed-capture",
      client_mutation_id: "reviewed-capture", view_id: "wb.journal.main",
      instance_id: JOURNAL_WIDGET_INSTANCE_IDS.capture,
      payload: { day_id: day.dayId, target_id: "running_notes", mode: "smart",
        exact_text: "  reviewed exact source  ", ...disclosure },
    });
    return operation === "submit" ? submit : { ...submit, intent_type: "wb.capture.retry-requested",
      payload: { capture_id: smartCaptureId, expected_revision: 4, ...disclosure } };
  }

  it.each(["submit", "retry"] as const)("forwards the clicked disclosure for %s even after the provider snapshot changes", async (operation) => {
    const digest = (value: string) => createHash("sha256").update(value).digest("hex");
    const disclosureHash = (fixture: ReturnType<typeof smartFixture>) => {
      const disclosure = fixture.view.capture.smartAvailability.disclosure;
      return digest(JSON.stringify({ maxInputBytes: disclosure.maxInputBytes, model: disclosure.model,
        provider: disclosure.provider, tools: disclosure.tools, web: disclosure.web }));
    };
    let fixture = smartFixture();
    const clickedHash = disclosureHash(fixture);
    let gestureBody: Record<string, unknown> | undefined;
    let postBody: Record<string, unknown> | undefined;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/local-identity/session/csrf") {
        return json({ ok: true, authenticated: true, principal: principal(), csrf_token: "csrf" });
      }
      if (url === LEGACY_TODAY_ENDPOINT) return json(legacy);
      if (url === JOURNAL_VIEW_ENDPOINT) return json(fixture);
      if (url === "/api/local-identity/gestures") {
        gestureBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return json({ ok: true, gesture: { token: "reviewed-gesture", action: gestureBody.action,
          subject_sha256: "s".repeat(64), context_sha256: gestureBody.context_sha256,
          expires_at: Date.now() / 1000 + 30 } });
      }
      if (url === JOURNAL_CAPTURE_ENDPOINT || url === `${JOURNAL_CAPTURE_ENDPOINT}/${smartCaptureId}/retry`) {
        postBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return json({ ok: true, persisted: true, capture: fixture.view.capture.recentSubmissions[0] });
      }
      throw new Error(`Unexpected ${url}`);
    });
    const provider = new HttpJournalProvider({ fetchImpl });
    await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    const intent = smartIntent(operation, clickedHash);
    // Contribution schemas identify the versioned intent; the host boundary
    // admits JSON payloads, while the owning provider validates these fields.
    expect(() => assertDashboardIntent(intent, JOURNAL_VIEW_DEFINITION_ID)).not.toThrow();
    fixture = smartFixture("swapped-model", "swapped-provider");
    await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "refresh" });

    expect((await provider.dispatch(intent)).status).toBe("accepted");
    expect(postBody?.smart_disclosure_sha256).toBe(clickedHash);
    expect(postBody?.smart_disclosure_sha256).not.toBe(disclosureHash(fixture));
    const expectedContext = operation === "submit"
      ? await journalCaptureGestureContext({ client_mutation_id: "reviewed-capture", day_id: day.dayId,
        target_id: "running_notes", mode: "smart", exact_text: "  reviewed exact source  ",
        input_mode: "unknown", smart_disclosure_sha256: clickedHash })
      : digest(`wb.journal-capture-retry/v1:${smartCaptureId}:4:${clickedHash}`);
    expect(gestureBody?.context_sha256).toBe(expectedContext);
  });

  it.each([
    ["submit", undefined], ["submit", "invalid"], ["submit", "A".repeat(64)],
    ["retry", undefined], ["retry", "invalid"], ["retry", "A".repeat(64)],
  ] as const)("rejects %s with a missing or noncanonical disclosure %s before authorizing or writing", async (operation, hash) => {
    const fetchImpl = fixtureFetch(smartFixture());
    const provider = new HttpJournalProvider({ fetchImpl });
    await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    fetchImpl.mockClear();

    const result = await provider.dispatch(smartIntent(operation, hash));

    expect(result.status).toBe("rejected");
    expect(result.message).toMatch(/Review the current Smart disclosure/);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("projects typed Smart disclosure and a provider-validated proposal link", async () => {
    const provider = new HttpJournalProvider({ fetchImpl: fixtureFetch(proposalFixture()) });
    const result = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    const capture = result.model?.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture];
    expect(capture?.smartAvailability).toEqual(disclosed);
    expect(capture?.recentSubmissions[0].followUps).toEqual([proposalFollowUp]);
  });

  it("normalizes the native newest-first window so the latest proposal remains visible", async () => {
    const fixture = proposalFixture();
    const newest = fixture.view.capture.recentSubmissions[0];
    fixture.view.capture.recentSubmissions = [newest,
      { ...newest, clientMutationId: "older", submittedAt: "2026-08-09T19:00:00-04:00", followUps: [] },
      { ...newest, clientMutationId: "oldest", submittedAt: "2026-08-09T18:00:00-04:00", followUps: [] }];
    const provider = new HttpJournalProvider({ fetchImpl: fixtureFetch(fixture) });
    const result = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    expect(result.model?.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture].recentSubmissions.map((item) => item.clientMutationId))
      .toEqual(["oldest", "older", newest.clientMutationId]);
  });

  it.each(["https://example.com/app/tasks?proposal=th-0123abcd", "/app/tasks?proposal=th-99999999",
    "/app/tasks?proposal=th-0123abcd&next=https://example.com", "/app/jobs?proposal=th-0123abcd",
    "/app/tasks?task=not-a-task", "/app/tasks/../../outside"])("rejects unsafe/mismatched domain follow-up %s", async (href) => {
    const provider = new HttpJournalProvider({ fetchImpl: fixtureFetch(proposalFixture(href)) });
    await expect(provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" })).rejects.toThrow(/unsafe follow-up/);
  });

  it("binds the displayed disclosure and explicit proposal action into the exact human gesture", async () => {
    const payload = { client_mutation_id: "capture-contract", day_id: day.dayId, exact_text: "  exact\nsource  ",
      input_mode: "paste", mode: "smart", target_id: "auto", smart_disclosure_sha256: "a".repeat(64) };
    const digest = (value: string) => createHash("sha256").update(value).digest("hex");
    const canonical = JSON.stringify({ client_mutation_id: payload.client_mutation_id, day_id: payload.day_id,
      exact_text_sha256: digest(payload.exact_text), input_mode: "paste", mode: "smart",
      schema: "wb.journal-capture-gesture/v1", smart_disclosure_sha256: payload.smart_disclosure_sha256,
      stated_at: null, target_id: "auto" });
    expect(await journalCaptureGestureContext(payload)).toBe(digest(canonical));
    expect(await journalCaptureGestureContext({ ...payload, smart_disclosure_sha256: "b".repeat(64) })).not.toBe(digest(canonical));
    const direct = { ...payload, mode: "dumb", target_id: "running_notes", smart_disclosure_sha256: undefined };
    expect(await journalCaptureGestureContext({ ...direct, follow_up_action: "task_proposal" }))
      .not.toBe(await journalCaptureGestureContext(direct));
  });

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

  it("reports a durably queued cutover capture as accepted", async () => {
    const queuedMessage = "The capture is saved and queued while Journal cutover maintenance finishes.";
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/local-identity/session/csrf") {
        return json({ ok: true, authenticated: true, principal: principal(), csrf_token: "csrf-token" });
      }
      if (url === "/api/local-identity/gestures") {
        const body = JSON.parse(String(init?.body)) as { context_sha256: string };
        return json({
          ok: true,
          gesture: {
            token: "gesture-token",
            action: "journal.capture.submit",
            subject_sha256: "s".repeat(64),
            context_sha256: body.context_sha256,
            expires_at: Date.now() / 1000 + 30,
          },
        });
      }
      if (url === JOURNAL_CAPTURE_ENDPOINT) {
        return json({
          ok: true,
          persisted: true,
          queued: true,
          deduplicated: false,
          capture: null,
          message: queuedMessage,
        }, 202);
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
      intent_id: "capture-queued",
      client_mutation_id: "capture-queued",
      view_id: "wb.journal.main",
      instance_id: JOURNAL_WIDGET_INSTANCE_IDS.capture,
      payload: {
        day_id: day.dayId,
        target_id: "running_notes",
        mode: "dumb",
        exact_text: "capture retained during maintenance",
      },
    };

    const result = await provider.dispatch(toDashboardJournalIntent(intent));

    expect(result).toMatchObject({ status: "accepted", message: queuedMessage });
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
