import { describe, expect, expectTypeOf, it } from "vitest";

import type {
  ViewSnapshot,
  WidgetInstanceId,
  WidgetIntent,
  WidgetSlotId,
  WidgetTypeId,
} from "./contracts";
import {
  asViewId,
  asWidgetInstanceId,
  asWidgetSlotId,
  asWidgetTypeId,
} from "./contracts";

describe("dashboard contribution contracts", () => {
  it("keeps widget type, slot, and instance identities nominally distinct", () => {
    expectTypeOf<WidgetTypeId>().not.toEqualTypeOf<WidgetSlotId>();
    expectTypeOf<WidgetSlotId>().not.toEqualTypeOf<WidgetInstanceId>();
    expectTypeOf<WidgetTypeId>().not.toEqualTypeOf<WidgetInstanceId>();

    expect(asWidgetTypeId("wb.capture.quick-text")).toBe("wb.capture.quick-text");
    expect(asWidgetSlotId("capture")).toBe("capture");
    expect(asWidgetInstanceId("default:capture")).toBe("default:capture");
  });

  it("accepts ordinary typed domain models without a JsonValue index signature", () => {
    interface TypedJournalModel {
      readonly title: string;
      readonly optionalNote?: string;
    }

    const snapshot: ViewSnapshot<TypedJournalModel> = {
      viewId: asViewId("wb.journal.main"),
      revision: 1,
      observedAt: "2026-07-12T12:00:00Z",
      status: "ready",
      quality: { kind: "demo" },
      model: { title: "Journal" },
      bindings: {},
      widgetInputs: {},
    };

    expect(snapshot.model.title).toBe("Journal");
  });

  it("lets a domain intent refine its payload while preserving the standard envelope", () => {
    interface CapturePayload {
      readonly text: string;
      readonly mode: "smart" | "dumb";
    }

    const intent: WidgetIntent<CapturePayload> = {
      intent_type: "wb.journal.capture-submitted",
      schema_version: 1,
      intent_id: "intent-1",
      client_mutation_id: "mutation-1",
      view_id: asViewId("wb.journal.main"),
      instance_id: asWidgetInstanceId("default:capture"),
      payload: { text: "Keep exact text", mode: "smart" },
    };

    expect(JSON.parse(JSON.stringify(intent))).toEqual(intent);
  });
});
