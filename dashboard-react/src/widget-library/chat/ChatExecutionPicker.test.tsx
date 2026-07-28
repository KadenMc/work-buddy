import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../test/setup";
import { ChatExecutionPicker } from "./ChatExecutionPicker";
import type { ChatExecutionSnapshot } from "./contracts";
import type { ChatExecutionControl } from "./useChatExecutionProfile";

const SNAPSHOT: ChatExecutionSnapshot = {
  selection: {
    providerId: "claude-code",
    modelId: "sonnet",
    providerLabel: "Claude Code",
    modelLabel: "Sonnet",
    revision: "execution:7",
  },
  providers: [
    {
      id: "claude-code",
      label: "Claude Code",
      available: true,
      authMode: "subscription",
      description: "Uses your signed-in Claude account.",
      models: [
        {
          id: "sonnet",
          label: "Sonnet",
          available: true,
          description: "Balanced speed and capability",
        },
      ],
    },
    {
      id: "codex",
      label: "Codex",
      available: true,
      authMode: "chatgpt",
      models: [
        {
          id: "gpt-5.6",
          label: "GPT-5.6",
          available: true,
          description: "Most capable",
        },
        {
          id: "gpt-retired",
          label: "Retired model",
          available: false,
          unavailableReason: "No longer offered by Codex",
        },
      ],
    },
  ],
};

const control = (
  overrides: Partial<ChatExecutionControl> = {},
): ChatExecutionControl => ({
  snapshot: SNAPSHOT,
  status: "ready",
  selecting: false,
  error: null,
  announcement: null,
  currentAvailable: true,
  select: vi.fn(async () => {}),
  retry: vi.fn(),
  ...overrides,
});

describe("ChatExecutionPicker", () => {
  it("selects one provider/model pair atomically from grouped choices", async () => {
    const execution = control();
    const { container } = render(
      <ChatExecutionPicker control={execution} />,
    );

    const trigger = screen.getByRole("button", {
      name: "Run with Claude Code · Sonnet",
    });
    expect(trigger).toBeVisible();
    await userEvent.click(trigger);

    expect(screen.getByText("Claude Code")).toBeVisible();
    expect(screen.getByText("Codex")).toBeVisible();
    expect(
      screen.getByText("Uses your signed-in Claude account."),
    ).toBeVisible();
    const unavailable = screen.getByRole("option", {
      name: /Codex, Retired model, No longer offered by Codex/,
    });
    expect(unavailable).toHaveAttribute("aria-disabled", "true");

    await userEvent.click(
      screen.getByRole("option", {
        name: "Codex, GPT-5.6, Most capable",
      }),
    );
    expect(execution.select).toHaveBeenCalledWith("codex", "gpt-5.6");
    await expectNoAccessibilityViolations(container);
  });

  it("renders truthful noninteractive metadata in a read-only chat", () => {
    render(<ChatExecutionPicker control={control()} readOnly />);

    expect(
      screen.getByLabelText("Run with Claude Code · Sonnet"),
    ).toHaveTextContent("Run withClaude Code · Sonnet");
    expect(
      screen.queryByRole("button", { name: /Run with/ }),
    ).not.toBeInTheDocument();
  });

  it("honors server read-only state without a misleading dropdown", () => {
    render(
      <ChatExecutionPicker
        control={control({
          snapshot: { ...SNAPSHOT, readOnly: true },
        })}
      />,
    );

    expect(
      screen.getByLabelText("Run with Claude Code · Sonnet"),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /Run with/ }),
    ).not.toBeInTheDocument();
  });

  it("keeps a failed load recoverable", async () => {
    const retry = vi.fn();
    render(
      <ChatExecutionPicker
        control={control({
          snapshot: null,
          status: "error",
          currentAvailable: false,
          error: "Models could not be reached.",
          retry,
        })}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Models could not be reached.",
    );
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalledOnce();
  });

  it("confirms a host-declared active-agent consequence before switching", async () => {
    const execution = control({
      confirmSelection: ({ providerLabel, modelLabel }) => ({
        title: `Switch to ${providerLabel} · ${modelLabel}?`,
        description:
          "This restarts the assistant with the new model. Your messages and draft stay here.",
        confirmLabel: "Switch",
      }),
    });
    render(<ChatExecutionPicker control={execution} />);
    const trigger = screen.getByRole("button", {
      name: "Run with Claude Code · Sonnet",
    });

    await userEvent.click(trigger);
    await userEvent.click(
      screen.getByRole("option", {
        name: "Codex, GPT-5.6, Most capable",
      }),
    );

    const dialog = screen.getByRole("dialog", {
      name: "Switch to Codex · GPT-5.6?",
    });
    expect(dialog).toHaveTextContent(
      "This restarts the assistant with the new model. Your messages and draft stay here.",
    );
    expect(execution.select).not.toHaveBeenCalled();
    await expectNoAccessibilityViolations(dialog);

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(execution.select).not.toHaveBeenCalled();

    await userEvent.click(trigger);
    await userEvent.click(
      screen.getByRole("option", {
        name: "Codex, GPT-5.6, Most capable",
      }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Switch" }));
    expect(execution.select).toHaveBeenCalledWith("codex", "gpt-5.6");
  });

  it("explains an unavailable current selection and offers a refresh", async () => {
    const retry = vi.fn();
    const unavailable: ChatExecutionSnapshot = {
      ...SNAPSHOT,
      selection: {
        providerId: "codex",
        modelId: "gpt-5.6",
        providerLabel: "Codex",
        modelLabel: "GPT-5.6",
        revision: "execution:8",
      },
      providers: SNAPSHOT.providers.map((provider) =>
        provider.id === "codex"
          ? {
              ...provider,
              available: false,
              unavailableReason: "Sign in to Codex with ChatGPT",
            }
          : provider,
      ),
    };
    const { rerender } = render(
      <ChatExecutionPicker
        control={control({
          snapshot: unavailable,
          currentAvailable: false,
          retry,
        })}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(
      "Unavailable: Sign in to Codex with ChatGPT",
    );
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));
    expect(retry).toHaveBeenCalledOnce();

    rerender(
      <ChatExecutionPicker
        readOnly
        control={control({
          snapshot: unavailable,
          currentAvailable: false,
          retry,
        })}
      />,
    );
    expect(screen.getByText(/Unavailable: Sign in to Codex/)).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Check again" }),
    ).not.toBeInTheDocument();
  });
});
