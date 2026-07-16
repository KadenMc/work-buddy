import { beforeEach, describe, expect, it } from "vitest";

import {
  asViewId,
  asWidgetInstanceId,
  asWidgetTypeId,
} from "../contributions/contracts";
import type { ViewPersonalizationPatch } from "./contracts";
import {
  InMemoryPersonalizationRepository,
  LocalStoragePersonalizationRepository,
  PersonalizationRepositoryError,
  parsePersonalizationPatch,
} from "./repository";

const viewId = asViewId("example.personalization.main");
const patch = (): ViewPersonalizationPatch => ({
  schemaVersion: 1,
  viewId,
  baseDefinitionVersion: 3,
  defaultSlotOverrides: {},
  addedInstances: [
    {
      instanceId: asWidgetInstanceId("wi_one"),
      widgetTypeId: asWidgetTypeId("example.personalization.card"),
      widgetDefinitionVersion: 1,
      settings: { filter: "open" },
      settingsSchemaVersion: 1,
      bindings: {},
      bindingVersion: 1,
      visibility: "shown",
      layout: {
        instanceId: asWidgetInstanceId("wi_one"),
        x: 0,
        y: 0,
        w: 8,
        h: 4,
      },
    },
  ],
  orphanedInstances: [],
  mobileOrderOverride: [asWidgetInstanceId("wi_one")],
});

beforeEach(() => localStorage.clear());

describe("personalization repositories", () => {
  it("round-trips a portable patch and Reset deletes it", async () => {
    const repository = new LocalStoragePersonalizationRepository(localStorage, "test.patch");
    await repository.save(patch());
    await expect(repository.load(viewId)).resolves.toEqual(patch());
    await repository.reset(viewId);
    await expect(repository.load(viewId)).resolves.toBeNull();
  });

  it("preserves corrupt raw state for recovery instead of silently deleting it", async () => {
    const repository = new LocalStoragePersonalizationRepository(localStorage, "test.corrupt");
    localStorage.setItem(`test.corrupt:${encodeURIComponent(viewId)}`, "{broken");

    const error = await repository.load(viewId).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(PersonalizationRepositoryError);
    expect(error).toMatchObject({ rawValue: "{broken" });
    expect(localStorage.length).toBe(1);
  });

  it("rejects leaked RGL fields at the persistence boundary", () => {
    const serialized = JSON.stringify(patch()).replace(
      '"instanceId":"wi_one","x"',
      '"instanceId":"wi_one","i":"wi_one","x"',
    );
    expect(() => parsePersonalizationPatch(serialized)).toThrow(/RGL-only/);
  });

  it("offers the same replaceable repository seam without browser storage", async () => {
    const repository = new InMemoryPersonalizationRepository();
    await repository.save(patch());
    const loaded = await repository.load(viewId);
    expect(loaded).toEqual(patch());
    expect(loaded).not.toBe(patch());
  });
});
