import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, useState } from "react";
import { describe, expect, it } from "vitest";

import { asWidgetInstanceId } from "../../contributions/contracts";
import { DurableCell } from "./DurableCell";
import { DurableWidgetHost, type DurableEntry } from "./DurableWidgetHost";

const INSTANCE = asWidgetInstanceId("wb.test.durable-instance");

/**
 * A stand-in for a live durable widget. It carries React state (the counter),
 * an uncontrolled input (its value lives only in the real DOM node), and a
 * stable testid so a test can prove the exact same DOM node is preserved.
 */
function Probe() {
  const [count, setCount] = useState(0);
  return (
    <div data-testid="probe">
      <span data-testid="count">{count}</span>
      <button type="button" onClick={() => setCount((value) => value + 1)}>
        bump
      </button>
      <input data-testid="probe-input" defaultValue="" />
    </div>
  );
}

function Harness({
  showCell,
  entries,
  cellKey = "only",
}: {
  showCell: boolean;
  entries: readonly DurableEntry[];
  cellKey?: string;
}) {
  return (
    <DurableWidgetHost entries={entries}>
      {showCell ? (
        <div key={cellKey} data-testid="cell-parent">
          <DurableCell instanceId={INSTANCE} />
        </div>
      ) : null}
    </DurableWidgetHost>
  );
}

const entryList = (): DurableEntry[] => [{ instanceId: INSTANCE, node: <Probe /> }];

describe("DurableWidgetHost", () => {
  it("keeps the same DOM node, React state, and uncontrolled input across a keyed remount of the cell parent", async () => {
    const user = userEvent.setup();
    const entries = entryList();
    const { rerender } = render(
      <Harness showCell entries={entries} cellKey="a" />,
    );

    const before = screen.getByTestId("probe");
    const tagged = before as HTMLElement & { __durableTag?: string };
    tagged.__durableTag = "kept";

    await user.click(screen.getByRole("button", { name: "bump" }));
    expect(screen.getByTestId("count")).toHaveTextContent("1");

    const input = screen.getByTestId("probe-input") as HTMLInputElement;
    await user.type(input, "hello");
    expect(input.value).toBe("hello");

    // Remount the cell's parent under a new key. The old cell is torn down and a
    // fresh one is mounted, so the placeholder is replaced entirely.
    rerender(<Harness showCell entries={entries} cellKey="b" />);

    const after = screen.getByTestId("probe");
    expect(after).toBe(before);
    expect((after as HTMLElement & { __durableTag?: string }).__durableTag).toBe(
      "kept",
    );
    expect(screen.getByTestId("count")).toHaveTextContent("1");
    expect((screen.getByTestId("probe-input") as HTMLInputElement).value).toBe(
      "hello",
    );
  });

  it("restores focus to the element that was focused when the cell was released", async () => {
    const entries = entryList();
    const { rerender } = render(
      <Harness showCell entries={entries} cellKey="a" />,
    );

    const input = screen.getByTestId("probe-input") as HTMLInputElement;
    input.focus();
    expect(document.activeElement).toBe(input);

    rerender(<Harness showCell entries={entries} cellKey="b" />);

    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByTestId("probe-input"));
    });
    // Identity is preserved, so the refocused input is the very same node.
    expect(screen.getByTestId("probe-input")).toBe(input);
  });

  it("does not move focus on a remount when nothing inside was focused", () => {
    const entries = entryList();
    const { rerender } = render(
      <Harness showCell entries={entries} cellKey="a" />,
    );
    expect(document.activeElement).toBe(document.body);

    rerender(<Harness showCell entries={entries} cellKey="b" />);

    // No release captured a focused element, so adopt schedules no restore.
    expect(document.activeElement).toBe(document.body);
  });

  it("unmounts the portal and removes the wrapper when the entry is evicted", () => {
    const { rerender } = render(
      <Harness showCell entries={entryList()} cellKey="a" />,
    );
    expect(screen.getByTestId("probe")).toBeInTheDocument();
    expect(document.querySelector(".wb-durable-slot")).not.toBeNull();

    rerender(<Harness showCell entries={[]} cellKey="a" />);

    expect(screen.queryByTestId("probe")).toBeNull();
    expect(document.querySelector(".wb-durable-slot")).toBeNull();
  });

  it("re-adopts a wrapper parked when its cell unmounted with no successor", () => {
    const entries = entryList();
    const { rerender } = render(<Harness showCell entries={entries} />);

    const before = screen.getByTestId("probe");
    const firstCell = document.querySelector(".wb-durable-cell");
    expect(firstCell?.contains(before)).toBe(true);

    // The cell unmounts while the entry stays, so the wrapper parks in the stash
    // and the live element remains mounted offstage rather than being destroyed.
    rerender(<Harness showCell={false} entries={entries} />);
    const stash = document.querySelector(".wb-durable-stash");
    const parked = screen.getByTestId("probe");
    expect(parked).toBe(before);
    expect(stash?.contains(parked)).toBe(true);
    expect(document.querySelector(".wb-durable-cell")).toBeNull();

    // A later cell mounts and re-adopts the very same live element.
    rerender(<Harness showCell entries={entries} />);
    const after = screen.getByTestId("probe");
    expect(after).toBe(before);
    expect(document.querySelector(".wb-durable-cell")?.contains(after)).toBe(
      true,
    );
  });

  it("keeps one live element inside the cell under StrictMode double invocation", () => {
    render(
      <StrictMode>
        <Harness showCell entries={entryList()} />
      </StrictMode>,
    );

    const probes = screen.getAllByTestId("probe");
    expect(probes).toHaveLength(1);
    expect(document.querySelector(".wb-durable-cell")?.contains(probes[0]!)).toBe(
      true,
    );
  });

  it("renders an empty placeholder and never throws with no host above it", () => {
    expect(() => render(<DurableCell instanceId={INSTANCE} />)).not.toThrow();
    const cell = document.querySelector(".wb-durable-cell");
    expect(cell).not.toBeNull();
    expect(cell?.childNodes.length).toBe(0);
  });
});
