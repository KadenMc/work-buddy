import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import {
  CoworkPassageAction,
  CoworkRoutingNotices,
} from "./CoworkChatExtensions";

describe("Cowork chat extensions", () => {
  it("activates the exact passage target with a descriptive accessible name", async () => {
    const target = {
      spanId: "span-9",
      anchor: { exact: "too strong" },
    };
    const onActivate = vi.fn();

    render(
      <CoworkPassageAction
        link={{
          messageId: "message-9",
          evidenceId: "evidence-9",
          target,
        }}
        onActivate={onActivate}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", {
        name: 'Jump to passage: "too strong"',
      }),
    );
    expect(onActivate).toHaveBeenCalledWith(target);
  });

  it("keeps a long passage out of the accessible name", () => {
    const quote = `A ${"very ".repeat(40)}long passage`;
    render(
      <CoworkPassageAction
        link={{
          messageId: "message-long",
          evidenceId: "evidence-long",
          target: {
            spanId: "span-long",
            anchor: { exact: quote },
          },
        }}
        onActivate={vi.fn()}
      />,
    );

    const button = screen.getByRole("button", { name: /Jump to passage:/ });
    expect(button.getAttribute("aria-label")?.length).toBeLessThanOrEqual(120);
    expect(button).toHaveAccessibleName(/…"/);
  });

  it("renders and dismisses delivered, queued, and failed routing notices", async () => {
    const onDismiss = vi.fn();
    render(
      <CoworkRoutingNotices
        deliveries={[
          {
            id: "routing-1",
            verb: "redirect",
            proposalId: "proposal-1",
            state: "delivered",
          },
          {
            id: "routing-2",
            verb: "endorse",
            proposalId: "proposal-2",
            state: "queued",
          },
          {
            id: "routing-3",
            verb: "redirect",
            proposalId: "proposal-3",
            state: "failed",
            reason: "conversation unavailable",
          },
        ]}
        onDismiss={onDismiss}
      />,
    );

    expect(
      screen.getByText("Redirect sent to the document agent."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Endorsement saved in chat. Restart chat to continue."),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Redirect could not be saved in chat/),
    ).toBeInTheDocument();
    expect(screen.getByText(/conversation unavailable/)).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", {
        name: "Dismiss delivered redirect notice",
      }),
    );
    expect(onDismiss).toHaveBeenCalledWith("routing-1");
  });

  it("does not create a nested live region and remains axe-clean", async () => {
    const { container } = render(
      <div role="log" aria-label="Conversation">
        <CoworkRoutingNotices
          deliveries={[
            {
              id: "routing-1",
              verb: "redirect",
              proposalId: "proposal-1",
              state: "failed",
            },
          ]}
        />
      </div>,
    );

    expect(container.querySelector("[role='status']")).toBeNull();
    expect(container.querySelector("[role='alert']")).toBeNull();
    await expectNoAccessibilityViolations(container);
  });
});
