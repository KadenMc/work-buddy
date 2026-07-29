import { useState } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import type {
  CoworkVerifyCapability,
  VerificationConfiguration,
} from "../rail/contracts";
import {
  VerifyCheckControl,
  type VerifyCheckControlProps,
  type VerifyCheckPage,
} from "./VerifyCheckControl";

const capability: CoworkVerifyCapability = {
  enabled: true,
  contractVersion: 1,
  canRun: true,
  canConfigure: true,
  canCothink: true,
  disabledReason: null,
};

const configuration: VerificationConfiguration = {
  schema: "work-buddy.cowork-verify-configuration/v1",
  documentId: "doc-1",
  executionPlan: null,
  coordination: {
    required: true,
    selection: "explicit_provider_and_model_at_run_start",
    contentBoundary: "entire_frozen_document",
    egressClass: "account_backed_agent",
    externalEgress: true,
    costCeilingUsdPerWorker: 2,
    separateReviserForFindings: true,
    pattern: "coordinator_then_optional_reviser_then_coordinator",
    baseWorkerCalls: 1,
    maximumWorkerCalls: 3,
  },
  criteria: [
    {
      id: "criterion-1",
      stableKey: "terminology_exact_match",
      version: 1,
      title: "Preferred terminology",
      description: "Use the configured preferred established term.",
      kind: "terminology",
      definitionOrigin: "system",
      author: { kind: "system", ref: "cowork-verify", meta: null },
      activationId: "activation-1",
      enabled: true,
      required: false,
      locked: false,
      activationOrigin: "system",
      authorizedBy: { kind: "system", ref: "cowork-verify", meta: null },
      operationalState: "active",
      availableCheckCount: 1,
      totalCheckCount: 1,
      issues: [],
      checks: [
        {
          id: "check-1",
          stableKey: "terminology_exact_match",
          version: 1,
          title: "Terminology exact-match check",
          mechanism: "deterministic",
          executorRef: "local:exact-match",
          limitations: ["Exact, case-sensitive matching only."],
          definitionOrigin: "system",
          author: { kind: "system", ref: "cowork-verify", meta: null },
          dataSharingClass: "local_only",
          externalEgress: false,
          dataSharingBasis: "admitted_deterministic_executor",
          availability: "available",
          unavailableReason: null,
          executionLocation: "local",
          bindingId: "binding-1",
          selected: true,
          configuration: {},
        },
      ],
    },
  ],
};

type HarnessProps = Omit<
  VerifyCheckControlProps,
  "page" | "onPageChange"
> & {
  readonly initialPage?: VerifyCheckPage;
};

function Harness({ initialPage = "select", ...props }: HarnessProps) {
  const [page, setPage] = useState<VerifyCheckPage>(initialPage);
  return (
    <VerifyCheckControl
      {...props}
      page={page}
      onPageChange={setPage}
    />
  );
}

describe("VerifyCheckControl", () => {
  it("keeps both the selection and replacement Add pages accessible", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <Harness
        capability={capability}
        configuration={configuration}
        onSetEnabled={vi.fn().mockResolvedValue(undefined)}
        onCreateCheck={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    await expectNoAccessibilityViolations(container);
    await user.click(screen.getByRole("button", { name: "Add check" }));
    await expectNoAccessibilityViolations(container);
    expect(
      screen.getByRole("button", { name: "Close add check" }),
    ).toBeVisible();
  });

  it("presents one compact check menu without executor internals", async () => {
    const user = userEvent.setup();
    render(
      <Harness
        capability={capability}
        configuration={configuration}
        onSetEnabled={vi.fn().mockResolvedValue(undefined)}
        onCreateCheck={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    expect(
      screen.getByRole("region", { name: "Verification checks" }),
    ).toBeVisible();
    expect(screen.getByText("1 selected")).toBeVisible();
    expect(screen.getByRole("button", { name: "Add check" })).toBeVisible();

    await user.click(screen.getByText("Checks"));
    expect(screen.getByText("Preferred terminology")).toBeVisible();
    expect(
      screen.getByText("Use the configured preferred established term."),
    ).toBeVisible();
    expect(screen.getByText("Built in")).toBeVisible();
    expect(
      screen.queryByText(/executor|egress|activation|criterion/u),
    ).not.toBeInTheDocument();
  });

  it("submits the exact activation precondition when a check is toggled", async () => {
    const user = userEvent.setup();
    const onSetEnabled = vi.fn().mockResolvedValue(undefined);
    render(
      <Harness
        capability={capability}
        configuration={configuration}
        onSetEnabled={onSetEnabled}
      />,
    );

    await user.click(screen.getByText("Checks"));
    await user.click(
      screen.getByRole("checkbox", {
        name: "Preferred terminology: include in Verify runs",
      }),
    );
    expect(onSetEnabled).toHaveBeenCalledWith(
      "terminology_exact_match",
      false,
      "activation-1",
    );
  });

  it("reports selection changes as busy until authoritative state settles", async () => {
    const user = userEvent.setup();
    let settleMutation!: () => void;
    const onSetEnabled = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          settleMutation = resolve;
        }),
    );
    const onBusyChange = vi.fn();
    render(
      <Harness
        capability={capability}
        configuration={configuration}
        onSetEnabled={onSetEnabled}
        onBusyChange={onBusyChange}
      />,
    );

    await user.click(screen.getByText("Checks"));
    await user.click(
      screen.getByRole("checkbox", {
        name: "Preferred terminology: include in Verify runs",
      }),
    );
    await waitFor(() => {
      expect(onBusyChange).toHaveBeenLastCalledWith(true);
    });

    await act(async () => settleMutation());
    await waitFor(() => {
      expect(onBusyChange).toHaveBeenLastCalledWith(false);
    });
  });

  it("replaces selection with Add check and X returns without losing the draft", async () => {
    const user = userEvent.setup();
    render(
      <Harness
        capability={capability}
        configuration={configuration}
        onCreateCheck={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Add check" }));
    expect(
      screen.getByRole("region", { name: "Add verification check" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("region", { name: "Verification checks" }),
    ).not.toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Name" }), "Voice");

    await user.click(screen.getByRole("button", { name: "Close add check" }));
    expect(
      screen.getByRole("region", { name: "Verification checks" }),
    ).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Add check" }));
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Voice");
  });

  it("saves a check and returns to selection", async () => {
    const user = userEvent.setup();
    const onCreateCheck = vi.fn().mockResolvedValue(undefined);
    render(
      <Harness
        capability={capability}
        configuration={configuration}
        onCreateCheck={onCreateCheck}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Add check" }));
    await user.type(screen.getByRole("textbox", { name: "Name" }), "Positive framing");
    await user.type(
      screen.getByRole("textbox", { name: "What should it check?" }),
      "Prefer direct positive descriptions.",
    );
    await user.type(
      screen.getByRole("textbox", { name: "Exceptions (optional)" }),
      "Negation can be necessary.",
    );
    await user.click(screen.getByRole("button", { name: "Save check" }));

    await waitFor(() =>
      expect(onCreateCheck).toHaveBeenCalledWith({
        title: "Positive framing",
        description: "Prefer direct positive descriptions.",
        evaluationInstructions: "Prefer direct positive descriptions.",
        limitations: ["Negation can be necessary."],
      }),
    );
    expect(
      screen.getByRole("region", { name: "Verification checks" }),
    ).toBeVisible();
  });

  it("keeps the Add check form intact when saving fails", async () => {
    const user = userEvent.setup();
    render(
      <Harness
        capability={capability}
        configuration={configuration}
        onCreateCheck={vi.fn().mockRejectedValue(new Error("Try again."))}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Add check" }));
    await user.type(screen.getByRole("textbox", { name: "Name" }), "Voice");
    await user.type(
      screen.getByRole("textbox", { name: "What should it check?" }),
      "Keep the voice consistent.",
    );
    await user.click(screen.getByRole("button", { name: "Save check" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Try again.");
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Voice");
  });
});
