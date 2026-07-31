import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { DashboardHelpProvider } from "../../../dashboard/help";
import { CoworkProvenanceForm } from "./CoworkProvenanceForm";
import {
  defaultCoworkProvenanceDetermination,
  type CoworkProvenanceDetermination,
} from "./contracts";

const ACTOR = {
  kind: "human",
  ref: "dashboard-user",
  identity_status: "local_actor_ref",
} as const;

function ControlledForm({
  help = false,
}: {
  readonly help?: boolean;
}) {
  const [value, setValue] = useState<CoworkProvenanceDetermination>(
    () => defaultCoworkProvenanceDetermination(ACTOR),
  );
  return (
    <DashboardHelpProvider enabled={help}>
      <CoworkProvenanceForm
        value={value}
        currentUserIdentity={ACTOR}
        onChange={setValue}
      />
      <output aria-label="Determination value">{JSON.stringify(value)}</output>
    </DashboardHelpProvider>
  );
}

const choose = async (
  user: ReturnType<typeof userEvent.setup>,
  field: string,
  option: string,
): Promise<void> => {
  await user.click(screen.getByRole("button", { name: new RegExp(field, "i") }));
  await user.click(screen.getByRole("option", { name: new RegExp(option, "i") }));
};

describe("CoworkProvenanceForm", () => {
  it("collects AI authorship, human review, and a named reviewer", async () => {
    const user = userEvent.setup();
    render(<ControlledForm />);

    await choose(user, "Authorship", "AI-written");
    expect(
      screen.queryByRole("button", { name: /^Author$/i }),
    ).not.toBeInTheDocument();
    await choose(user, "Human review", "Reviewed by a person");
    await choose(user, "Reviewer", "Someone else");
    await user.type(screen.getByRole("textbox", { name: "Reviewer’s name" }), "Morgan");

    expect(screen.getByLabelText("Determination value")).toHaveTextContent(
      '"kind":"ai"',
    );
    expect(screen.getByLabelText("Determination value")).toHaveTextContent(
      '"status":"reviewed"',
    );
    expect(screen.getByLabelText("Determination value")).toHaveTextContent(
      '"display_name":"Morgan"',
    );
  });

  it("collects a named human contributor for mixed authorship", async () => {
    const user = userEvent.setup();
    render(<ControlledForm />);

    await choose(user, "Authorship", "Human and AI");
    await choose(user, "Human contributor", "Someone else");
    await user.type(
      screen.getByRole("textbox", { name: "Author’s name" }),
      "Taylor",
    );
    await choose(user, "Human review", "Not reviewed");

    const value = screen.getByLabelText("Determination value");
    expect(value).toHaveTextContent('"kind":"mixed"');
    expect(value).toHaveTextContent('"display_name":"Taylor"');
    expect(value).toHaveTextContent('"status":"not_reviewed"');
  });

  it("captures the immutable actor ref when Me is selected again", async () => {
    const user = userEvent.setup();
    render(<ControlledForm />);

    await choose(user, "Author$", "Someone else");
    await user.type(
      screen.getByRole("textbox", { name: "Author’s name" }),
      "Taylor",
    );
    await choose(user, "Author$", "^Me$");

    expect(screen.getByLabelText("Determination value")).toHaveTextContent(
      '"ref":"dashboard-user"',
    );
    expect(screen.getByLabelText("Determination value")).toHaveTextContent(
      '"identity_status":"local_actor_ref"',
    );
  });

  it("does not flag a newly revealed named-person field before the user leaves it", async () => {
    const user = userEvent.setup();
    render(<ControlledForm />);

    await choose(user, "Author$", "Someone else");
    const name = screen.getByRole("textbox", { name: "Author’s name" });
    expect(name).not.toHaveAttribute("aria-invalid", "true");
    expect(screen.queryByText("Enter the author’s name.")).toBeNull();

    await user.click(name);
    await user.tab();
    expect(name).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Enter the author’s name.")).toBeVisible();
  });

  it("attaches hover help to the existing authorship control", async () => {
    const user = userEvent.setup();
    render(<ControlledForm help />);

    const authorship = screen.getByRole("button", { name: /Authorship/i });
    expect(authorship).toHaveAttribute("data-help-target", "true");
    await user.hover(authorship);
    expect(
      await screen.findByText("Record who created this text.", undefined, {
        timeout: 3_000,
      }),
    ).toBeVisible();
  });

  it("explains local and claimed identities on the existing Author and Reviewer controls", async () => {
    const user = userEvent.setup();
    render(<ControlledForm help />);

    const author = screen.getByRole("button", { name: /Author$/i });
    expect(author).toHaveAttribute("data-help-target", "true");
    await user.hover(author);
    expect(await screen.findByText("Identify the human author.")).toBeVisible();
    expect(
      screen.getByText(/current local dashboard actor.*claimed identity/),
    ).toBeVisible();
    await user.unhover(author);

    await choose(user, "Authorship", "AI-written");
    await choose(user, "Human review", "Reviewed by a person");
    const reviewer = screen.getByRole("button", { name: /Reviewer$/i });
    expect(reviewer).toHaveAttribute("data-help-target", "true");
    await user.hover(reviewer);
    expect(await screen.findByText("Identify the human reviewer.")).toBeVisible();
    expect(
      screen.getByText(/current local dashboard actor.*claimed identity/),
    ).toBeVisible();
  });
});
