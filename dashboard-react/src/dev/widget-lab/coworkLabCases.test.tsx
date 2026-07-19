import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { expectNoAccessibilityViolations } from "../../test/setup";
import { CoworkLabSection } from "./coworkLabCases";

describe("Co-work Widget Lab section", () => {
  it("renders every card type", () => {
    render(<CoworkLabSection />);
    expect(
      within(screen.getByTestId("cowork-lab-card-insertion")).getByText(
        "Insertion",
      ),
    ).toBeVisible();
    expect(
      within(screen.getByTestId("cowork-lab-card-deletion")).getByText(
        "Deletion",
      ),
    ).toBeVisible();
    expect(
      within(screen.getByTestId("cowork-lab-card-flag")).getByText("Flag"),
    ).toBeVisible();
    expect(
      within(screen.getByTestId("cowork-lab-card-claim")).getByText("Claim"),
    ).toBeVisible();
  });

  it("renders every verb group", () => {
    render(<CoworkLabSection />);
    const edit = within(screen.getByTestId("cowork-lab-markbar-edit"));
    for (const label of [
      "Accept",
      "Amend",
      "Reject",
      "Reject as false",
      "Reject as preference",
      "Redirect",
      "Defer",
    ]) {
      expect(edit.getByRole("button", { name: label })).toBeVisible();
    }

    const flag = within(screen.getByTestId("cowork-lab-markbar-flag"));
    for (const label of ["Endorse", "Dismiss", "Redirect"]) {
      expect(flag.getByRole("button", { name: label })).toBeVisible();
    }

    const claim = within(screen.getByTestId("cowork-lab-markbar-claim"));
    for (const label of [
      "Confirm",
      "Reject",
      "Challenge",
      "Supersede",
      "Redact",
      "Propose",
    ]) {
      expect(claim.getByRole("button", { name: label })).toBeVisible();
    }
  });

  it("disables the accept-family verbs on the stale-base state", () => {
    render(<CoworkLabSection />);
    const stale = within(screen.getByTestId("cowork-lab-stale"));
    // The card badge and the mark-bar note both state the stale reason in text.
    expect(stale.getByText("Stale base, reject or defer only")).toBeVisible();
    const markbar = within(screen.getByTestId("cowork-lab-markbar-stale"));
    expect(markbar.getByRole("button", { name: "Accept" })).toBeDisabled();
    expect(markbar.getByRole("button", { name: "Amend" })).toBeDisabled();
    expect(markbar.getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(markbar.getByRole("button", { name: "Defer" })).toBeEnabled();
  });

  it("renders the narrow grouped fallback", () => {
    render(<CoworkLabSection />);
    const grouped = within(screen.getByTestId("cowork-lab-grouped"));
    expect(grouped.getByRole("region", { name: "Suggestions" })).toBeVisible();
    expect(grouped.getByRole("region", { name: "Flags" })).toBeVisible();
    expect(grouped.getByRole("region", { name: "Claims" })).toBeVisible();
  });

  it("stages a verb through the live mark bar", async () => {
    render(<CoworkLabSection />);
    const edit = within(screen.getByTestId("cowork-lab-markbar-edit"));
    const accept = edit.getByRole("button", { name: "Accept" });
    expect(accept).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(accept);
    expect(accept).toHaveAttribute("aria-pressed", "true");
  });

  it("clears axe on the card and grouped-fallback panels", async () => {
    // Per-component axe lives in the conformance suite. The whole section mounts
    // several mark bars at once, which collide only as a lab-composition artifact
    // (production shows one mark bar), so axe is scoped to panels here.
    render(<CoworkLabSection />);
    await expectNoAccessibilityViolations(
      screen.getByTestId("cowork-lab-card-insertion"),
    );
    await expectNoAccessibilityViolations(
      screen.getByTestId("cowork-lab-grouped"),
    );
  });
});
