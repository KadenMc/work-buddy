import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import { SlashMenu } from "./SlashMenu";
import { SLASH_COMMANDS, filterSlashCommands } from "./slashCommands";

// cmdk observes its list with ResizeObserver, which jsdom does not implement.
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver ??=
  ResizeObserverStub as unknown as typeof ResizeObserver;
// cmdk scrolls the active option into view, which jsdom does not implement.
if (typeof Element.prototype.scrollIntoView !== "function") {
  Element.prototype.scrollIntoView = function scrollIntoView() {};
}

describe("SlashMenu", () => {
  it("renders one item per command with a group heading", () => {
    render(
      <SlashMenu
        commands={filterSlashCommands("head")}
        activeId="heading-1"
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText("Heading 1")).toBeInTheDocument();
    expect(screen.getByText("Heading 2")).toBeInTheDocument();
    expect(screen.getByText("Heading 3")).toBeInTheDocument();
    expect(screen.getByText("Basic")).toBeInTheDocument();
  });

  it("calls onSelect with the clicked command", () => {
    const onSelect = vi.fn();
    render(
      <SlashMenu
        commands={filterSlashCommands("head")}
        activeId="heading-1"
        onSelect={onSelect}
      />,
    );
    fireEvent.click(screen.getByText("Heading 2"));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect.mock.calls[0][0].id).toBe("heading-2");
  });

  it("renders the empty state when nothing matches", () => {
    render(<SlashMenu commands={[]} activeId={null} onSelect={() => {}} />);
    expect(screen.getByText("No blocks match")).toBeInTheDocument();
  });

  it("shows every block group when the query is empty", () => {
    render(
      <SlashMenu
        commands={SLASH_COMMANDS}
        activeId="text"
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText("Basic")).toBeInTheDocument();
    expect(screen.getByText("Lists")).toBeInTheDocument();
    expect(screen.getByText("Blocks")).toBeInTheDocument();
    expect(screen.getByText("Media")).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <SlashMenu
        commands={SLASH_COMMANDS}
        activeId="text"
        onSelect={() => {}}
      />,
    );
    await expectNoAccessibilityViolations(container);
  });
});
