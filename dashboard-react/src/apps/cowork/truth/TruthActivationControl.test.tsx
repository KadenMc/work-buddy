import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CoworkDocumentCapabilityEnvelope } from "../contracts";
import {
  TruthActivationControl,
  truthActivationPlan,
  type TruthActivationClient,
} from "./TruthActivationControl";

const envelope = (
  activation: "disabled" | "enabled" | "paused",
  revision: number,
  ledgerPresent = false,
  eligibility: "allowed" | "required" = "allowed",
): CoworkDocumentCapabilityEnvelope => ({
  schema: "wb.cowork-document-capabilities/v1",
  interactionContract: {
    contractId: "work-buddy.working-document",
    version: 1,
    digest: "a".repeat(64),
  },
  modules: {
    review: true,
    provenance: true,
    chat: true,
    truth: activation !== "disabled",
  },
  truth: {
    eligibility,
    activation,
    activationRevision: revision,
    policyFingerprint: "policy",
    ledgerPresent,
    unavailableReason: null,
  },
});

describe("TruthActivationControl", () => {
  it("loads only when opened and requires deliberate confirmation before enabling", async () => {
    const user = userEvent.setup();
    const initial = envelope("disabled", 1);
    const enabled = envelope("enabled", 2);
    const client: TruthActivationClient = {
      loadActivationPolicy: vi.fn().mockResolvedValue({
        capabilityEnvelope: initial,
        documentHeadSha256: "b".repeat(64),
      }),
      transitionTruthActivation: vi.fn().mockResolvedValue({
        capabilityEnvelope: enabled,
        documentHeadSha256: "b".repeat(64),
      }),
    };
    const onChanged = vi.fn();
    render(
      <TruthActivationControl
        client={client}
        envelope={initial}
        onChanged={onChanged}
        intentIdFactory={() => "intent-enable"}
      />,
    );

    expect(client.loadActivationPolicy).not.toHaveBeenCalled();
    await user.click(screen.getByText("Truth settings"));
    await waitFor(() =>
      expect(client.loadActivationPolicy).toHaveBeenCalledTimes(1),
    );

    const action = screen.getByRole("button", { name: "Turn on Truth" });
    expect(action).toBeDisabled();
    await user.click(
      screen.getByRole("checkbox", {
        name: "I confirm this explicit Truth change.",
      }),
    );
    expect(action).toBeEnabled();
    await user.click(action);

    await waitFor(() =>
      expect(client.transitionTruthActivation).toHaveBeenCalledWith({
        nextState: "enabled",
        expectedActivationRevision: 1,
        expectedInteractionContractSha256: "a".repeat(64),
        expectedDocumentHeadSha256: "b".repeat(64),
        intentId: "intent-enable",
      }),
    );
    expect(onChanged).toHaveBeenLastCalledWith(enabled);
    expect(screen.getByText("Truth is now on.")).toBeInTheDocument();
  });

  it("keeps a CAS failure explicit and requires a fresh confirmation", async () => {
    const user = userEvent.setup();
    const initial = envelope("enabled", 4, true);
    const client: TruthActivationClient = {
      loadActivationPolicy: vi.fn().mockResolvedValue({
        capabilityEnvelope: initial,
        documentHeadSha256: "c".repeat(64),
      }),
      transitionTruthActivation: vi
        .fn()
        .mockRejectedValue(new Error("Truth activation changed after it was shown.")),
    };
    render(
      <TruthActivationControl
        client={client}
        envelope={initial}
        onChanged={() => undefined}
        intentIdFactory={() => "intent-pause"}
      />,
    );

    await user.click(screen.getByText("Truth settings"));
    await screen.findByRole("button", { name: "Pause Truth" });
    await user.click(
      screen.getByRole("checkbox", {
        name: "I confirm this explicit Truth change.",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Pause Truth" }));

    expect(
      await screen.findByText(
        "Truth activation changed after it was shown. Refresh the settings before trying again.",
      ),
    ).toHaveAttribute("role", "alert");
    expect(
      screen.getByRole("checkbox", {
        name: "I confirm this explicit Truth change.",
      }),
    ).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Pause Truth" })).toBeDisabled();
  });

  it("derives only the backend-approved transition for each policy state", () => {
    expect(truthActivationPlan(envelope("disabled", 1))?.nextState).toBe(
      "enabled",
    );
    expect(truthActivationPlan(envelope("enabled", 2))?.nextState).toBe(
      "disabled",
    );
    expect(
      truthActivationPlan(envelope("enabled", 2, true))?.nextState,
    ).toBe("paused");
    expect(
      truthActivationPlan(envelope("enabled", 2, false, "required"))
        ?.nextState,
    ).toBe("paused");
    expect(truthActivationPlan(envelope("paused", 3, true))?.nextState).toBe(
      "enabled",
    );
  });
});
