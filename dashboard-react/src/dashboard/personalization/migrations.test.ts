import { asWidgetTypeId, type JsonValue } from "../contributions/contracts";
import { describe, expect, it } from "vitest";

import {
  executeWidgetMigration,
  planWidgetMigration,
  type WidgetMigrationRequest,
  type WidgetMigrationStep,
  type WidgetStateVersion,
} from "./migrations";

const typeA = asWidgetTypeId("example.widget.a");
const typeB = asWidgetTypeId("example.widget.b");
const version = (
  widgetTypeId: typeof typeA | typeof typeB,
  definition: number,
  settings: number,
): WidgetStateVersion => ({
  widgetTypeId,
  widgetDefinitionVersion: definition,
  settingsSchemaVersion: settings,
  bindingVersion: 1,
});

const request = (
  target: WidgetStateVersion,
  allowExplicitReset = false,
): WidgetMigrationRequest => ({
  source: version(typeA, 1, 1),
  target,
  sourceState: { settings: { density: "compact" }, bindings: { project: "northwind" } },
  targetDefaults: { settings: { density: "comfortable" }, bindings: {} },
  allowExplicitReset,
});

describe("widget state migrations", () => {
  it("plans and executes a deterministic multi-step field migration", () => {
    const steps: readonly WidgetMigrationStep[] = [
      {
        id: "a-v1-v2",
        from: version(typeA, 1, 1),
        to: version(typeA, 2, 2),
        description: "Rename density to spacing",
        migrate: (state) => ({
          ...state,
          settings: { spacing: (state.settings as { density: string }).density },
        }),
      },
      {
        id: "a-v2-b-v1",
        from: version(typeA, 2, 2),
        to: version(typeB, 1, 1),
        description: "Adopt alternate card schema",
        migrate: (state) => ({
          ...state,
          settings: {
            ...(state.settings as Record<string, JsonValue>),
            variant: "b",
          },
        }),
      },
    ];

    const plan = planWidgetMigration(request(version(typeB, 1, 1)), steps);
    expect(plan).toMatchObject({ mode: "migrate" });
    expect(plan.steps.map((step) => step.id)).toEqual(["a-v1-v2", "a-v2-b-v1"]);
    expect(executeWidgetMigration(plan)).toMatchObject({
      ok: true,
      mode: "migrate",
      appliedStepIds: ["a-v1-v2", "a-v2-b-v1"],
      state: { settings: { spacing: "compact", variant: "b" } },
    });
  });

  it("requires explicit reset and preserves the raw source for recovery", () => {
    const unavailable = planWidgetMigration(request(version(typeB, 1, 1)), []);
    expect(executeWidgetMigration(unavailable)).toMatchObject({
      ok: false,
      recoverableSource: { settings: { density: "compact" } },
    });

    const reset = planWidgetMigration(request(version(typeB, 1, 1), true), []);
    expect(executeWidgetMigration(reset)).toMatchObject({
      ok: true,
      mode: "reset",
      state: { settings: { density: "comfortable" }, bindings: {} },
      recoverableSource: { settings: { density: "compact" } },
    });
  });

  it("turns a thrown or non-JSON migration into a recoverable failure", () => {
    const broken: WidgetMigrationStep = {
      id: "broken",
      from: version(typeA, 1, 1),
      to: version(typeB, 1, 1),
      description: "Broken migration",
      migrate: () => ({ settings: { invalid: Number.NaN }, bindings: {} }),
    };
    const result = executeWidgetMigration(
      planWidgetMigration(request(version(typeB, 1, 1)), [broken]),
    );
    expect(result).toMatchObject({
      ok: false,
      failedStepId: "broken",
      recoverableSource: { settings: { density: "compact" } },
    });
  });
});
