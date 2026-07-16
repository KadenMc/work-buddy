import { describe, expect, it } from "vitest";

import {
  asAppId,
  asViewId,
  asWidgetInstanceId,
  asWidgetTypeId,
} from "../contributions/contracts";
import { WidgetDraftConflictError, type WidgetDraftIdentity } from "./contracts";
import {
  ForkedWidgetDraftRepository,
  InMemoryWidgetDraftRepository,
} from "./repository";

const identity: WidgetDraftIdentity = {
  profileId: "person-1",
  workspaceId: "workspace-1",
  appId: asAppId("wb.capture"),
  viewId: asViewId("wb.journal.main"),
  instanceId: asWidgetInstanceId("journal:capture"),
  widgetTypeId: asWidgetTypeId("wb.capture.quick-text"),
  draftName: "capture",
  scopeKey: "2026-07-11",
};

describe("InMemoryWidgetDraftRepository", () => {
  it("round-trips structured drafts and enforces compare-and-set revisions", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const first = await repository.save({
      ...identity,
      draftSchema: { schemaId: "wb.capture.quick-text.draft", version: 1 },
      value: { text: "first", targetId: "log", mode: "smart" },
      retentionDays: 30,
    });
    expect(first.revision).toBe(1);
    expect(await repository.load(identity)).toMatchObject({
      revision: 1,
      value: { text: "first", targetId: "log", mode: "smart" },
    });

    const second = await repository.save({
      ...identity,
      draftSchema: first.draftSchema,
      value: { text: "second" },
      expectedRevision: first.revision,
    });
    expect(second.revision).toBe(2);
    await expect(
      repository.save({
        ...identity,
        draftSchema: first.draftSchema,
        value: { text: "stale writer" },
        expectedRevision: first.revision,
      }),
    ).rejects.toBeInstanceOf(WidgetDraftConflictError);
  });

  it("does not let a stale clear remove a newer draft", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const saved = await repository.save({
      ...identity,
      draftSchema: { schemaId: "wb.capture.quick-text.draft", version: 1 },
      value: { text: "keep me" },
    });
    await expect(repository.delete(identity, saved.revision - 1)).rejects.toBeInstanceOf(
      WidgetDraftConflictError,
    );
    expect(await repository.load(identity)).toMatchObject({ value: { text: "keep me" } });
  });
});

describe("ForkedWidgetDraftRepository", () => {
  it("reads the real draft but keeps preview saves and deletes disposable", async () => {
    const base = new InMemoryWidgetDraftRepository();
    const original = await base.save({
      ...identity,
      draftSchema: { schemaId: "wb.capture.quick-text.draft", version: 1 },
      value: { text: "real draft" },
    });
    const preview = new ForkedWidgetDraftRepository(base);

    expect(await preview.load(identity)).toMatchObject({ value: { text: "real draft" } });
    await preview.save({
      ...identity,
      draftSchema: original.draftSchema,
      value: { text: "preview only" },
      expectedRevision: original.revision,
    });
    expect(await preview.load(identity)).toMatchObject({ value: { text: "preview only" } });
    expect(await base.load(identity)).toMatchObject({ value: { text: "real draft" } });

    await preview.delete(identity);
    expect(await preview.load(identity)).toBeUndefined();
    expect(await base.load(identity)).toMatchObject({ value: { text: "real draft" } });
  });
});
