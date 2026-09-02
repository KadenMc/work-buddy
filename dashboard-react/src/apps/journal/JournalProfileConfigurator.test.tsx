import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  JournalProfileConfigurator,
  nextLocalCalendarDate,
} from "./JournalProfileConfigurator";
import type {
  JournalProfileConfiguration,
  JournalProfileConfigurationProvider,
  JournalProfileDraft,
  JournalProfileRevisionRecord,
} from "./profileConfiguration";

const editableProfile = {
  profileId: "user.focus",
  profileRevision: 3,
  formatVersion: 1,
  name: "Focus",
  description: "A focused reflection.",
  profileDigest: "focus-digest",
  createdBy: "person:test",
  createdAt: "1970-01-01T00:00:00Z",
  supersedesRevision: 2,
  editable: true,
  modules: [{
    slotId: "reflection",
    ordinal: 0,
    required: false,
    moduleInstanceId: "user.focus.reflection",
    moduleInstanceVersion: 5,
    moduleTypeId: "field_group",
    moduleTypeVersion: 1,
    label: "Reflection",
    settings: {},
    behaviorId: "human_value",
    behaviorVersion: 1,
    scheduleKind: "always",
    schedule: {},
    fields: [{
      slotId: "clarity",
      fieldId: "user.focus.clarity",
      fieldDefinitionVersion: 7,
      owner: "user",
      stableKey: "clarity",
      label: "Clarity",
      description: "",
      valueKind: "scale",
      unit: null,
      constraints: {},
      functionId: null,
      functionVersion: null,
      behaviorId: "human_value",
      behaviorVersion: 1,
      privacyClass: "private",
      searchMode: "structured_only",
      disclosurePolicyId: "private_default/v1",
      prompt: {
        promptId: "user.focus.clarity.prompt",
        promptVersion: 4,
        wording: "How clear is the next action?",
        helpText: "",
        requiredness: "optional",
        scheduleKind: "always",
        schedule: {},
      },
    }],
  }],
} satisfies JournalProfileRevisionRecord;

const builtInProfile = {
  ...editableProfile,
  profileId: "simple-journal",
  profileRevision: 1,
  name: "Simple Journal",
  profileDigest: "simple-digest",
  createdBy: "work-buddy",
  supersedesRevision: null,
  editable: false,
  modules: editableProfile.modules.map((module) => ({
    ...module,
    moduleInstanceId: "simple.reflection",
    moduleInstanceVersion: 1,
    fields: module.fields.map((field) => ({
      ...field,
      fieldId: "simple.clarity",
      fieldDefinitionVersion: 1,
      owner: "work-buddy",
      prompt: field.prompt === null ? null : {
        ...field.prompt,
        promptId: "simple.clarity.prompt",
        promptVersion: 1,
      },
    })),
  })),
} satisfies JournalProfileRevisionRecord;

const configuration = {
  schemaVersion: 1,
  authorityState: "database_only",
  activationRevision: 2,
  profiles: [builtInProfile, editableProfile],
  moduleTypes: [{ moduleTypeId: "field_group", moduleTypeVersion: 1, definition: {} }],
  behaviors: [{ behaviorId: "human_value", behaviorVersion: 1, definition: {} }],
  functions: [],
  valueKinds: ["scale"],
  scheduleKinds: ["always"],
} satisfies JournalProfileConfiguration;

const makeClient = () => {
  const save = vi.fn(async (draft: JournalProfileDraft) => ({
    profileId: draft.profileId,
    profileRevision: draft.expectedRevision + 1,
    profileDigest: "saved-digest",
    activationRevision: 2,
  }));
  const client: JournalProfileConfigurationProvider = {
    load: vi.fn(async () => configuration),
    preview: vi.fn(async () => { throw new Error("Unexpected preview"); }),
    save,
    activate: vi.fn(async (input) => ({
      activationRevision: input.expectedActivationRevision + 1,
      effectiveLocalDate: input.effectiveLocalDate,
    })),
  };
  return { client, save };
};

describe("JournalProfileConfigurator", () => {
  it("keeps tomorrow on the browser-local calendar near negative-offset UTC rollover", () => {
    const nearMidnightInNewYork = {
      getFullYear: () => 2026,
      getMonth: () => 7,
      getDate: () => 31,
    };
    const sameInstant = new Date("2026-08-31T23:30:00-04:00");

    expect(sameInstant.toISOString().slice(0, 10)).toBe("2026-09-01");
    expect(nextLocalCalendarDate(nearMidnightInNewYork)).toBe("2026-09-01");
  });

  it("normalizes local month and year rollover as a civil calendar date", () => {
    expect(nextLocalCalendarDate({
      getFullYear: () => 2026,
      getMonth: () => 11,
      getDate: () => 31,
    })).toBe("2027-01-01");
  });

  it("edits only user-owned profiles and submits their current CAS versions", async () => {
    const user = userEvent.setup();
    const { client, save } = makeClient();
    render(<JournalProfileConfigurator client={client} />);

    const builtInCard = (await screen.findByRole("heading", { name: "Simple Journal" }))
      .closest("article")!;
    const editableCard = screen.getByRole("heading", { name: "Focus" }).closest("article")!;
    expect(within(builtInCard).queryByRole("button", { name: "Edit profile" })).toBeNull();
    expect(within(builtInCard).getByRole("button", { name: "Fork as new profile" })).toBeVisible();

    await user.click(within(editableCard).getByRole("button", { name: "Edit profile" }));
    await user.click(screen.getByRole("button", { name: "Save revision" }));

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(save.mock.calls[0]?.[0]).toMatchObject({
      profileId: "user.focus",
      expectedRevision: 3,
      modules: [{
        slotId: "reflection",
        moduleInstanceId: "user.focus.reflection",
        expectedVersion: 5,
        fields: [{
          slotId: "clarity",
          fieldId: "user.focus.clarity",
          expectedVersion: 7,
          stableKey: "clarity",
          prompt: {
            promptId: "user.focus.clarity.prompt",
            expectedVersion: 4,
          },
        }],
      }],
    });
  });

  it("forks a built-in profile under fresh user-owned identities", async () => {
    const user = userEvent.setup();
    const { client, save } = makeClient();
    render(<JournalProfileConfigurator client={client} />);

    const builtInCard = (await screen.findByRole("heading", { name: "Simple Journal" }))
      .closest("article")!;
    await user.click(within(builtInCard).getByRole("button", { name: "Fork as new profile" }));
    await user.click(screen.getByRole("button", { name: "Save new profile" }));

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    const fork = save.mock.calls[0]?.[0];
    expect(fork).toMatchObject({
      expectedRevision: 0,
      modules: [{ expectedVersion: 0, fields: [{ expectedVersion: 0 }] }],
    });
    expect(fork?.profileId).toMatch(/^user\.profile\./u);
    expect(fork?.modules[0]?.moduleInstanceId).toMatch(/^user\.module\./u);
    expect(fork?.modules[0]?.fields[0]?.fieldId).toMatch(/^user\.field\./u);
    expect(fork?.modules[0]?.fields[0]?.prompt?.promptId).toMatch(/^user\.prompt\./u);
    expect(fork?.modules[0]?.fields[0]?.stableKey).not.toBe("clarity");
  });
});
