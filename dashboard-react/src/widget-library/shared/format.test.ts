import { describe, expect, it } from "vitest";

import { formatActorLabel } from "./format";

describe("formatActorLabel", () => {
  it.each([
    ["human", "Human"],
    ["agent_run", "AI run"],
    ["service", "Work Buddy"],
    ["system", "Work Buddy"],
  ])("formats canonical and legacy %s actors without exposing identity", (kind, label) => {
    const actorRef = JSON.stringify({
      schema: "wb.actor-ref/v1",
      kind,
      issuer_authority_id: "private-authority",
      subject: "private-subject",
      tenant_scope_id: "private-scope",
    });
    expect(formatActorLabel(actorRef)).toBe(label);
    expect(formatActorLabel(kind)).toBe(label);
  });

  it.each([
    null,
    undefined,
    "",
    "Owner",
    "You",
    "{malformed actor",
    '{"schema":"unknown","kind":"human"}',
    '{"schema":"wb.actor-ref/v1","kind":"unknown"}',
    '{"kind":"human"}',
    '["human"]',
    "42",
    "null",
    { schema: "wb.actor-ref/v1", kind: "human" },
  ])("uses a neutral label for unknown or malformed input %j", (value) => {
    expect(formatActorLabel(value)).toBe("Recorded actor");
  });
});
