import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { JournalViewChrome } from "../chrome/JournalViewChrome";
import { JULY11_INITIAL_MODEL } from "../fixtures/july11";
import { JOURNAL_OFFLINE_FIXTURE, JOURNAL_READ_ONLY_FIXTURE } from "../fixtures/states";

describe("JournalViewChrome", () => {
  it("labels the day boundary separately from the actual opened time and marks demo data", () => {
    render(
      createElement(JournalViewChrome, {
        day: JULY11_INITIAL_MODEL.day,
        access: JULY11_INITIAL_MODEL.access,
        quality: JULY11_INITIAL_MODEL.quality,
        source: JULY11_INITIAL_MODEL.source,
      }),
    );

    expect(screen.getByRole("heading", { name: "Journal" })).toBeInTheDocument();
    expect(screen.getByText("Day boundary 5:00 AM")).toBeInTheDocument();
    expect(screen.getByText("Opened 8:42 AM")).toBeInTheDocument();
    expect(screen.getByText("Demo data")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open previous Journal day" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Open next Journal day" })).toBeDisabled();
  });

  it("exposes explicit date-navigation actions", async () => {
    const user = userEvent.setup();
    const onNavigateDay = vi.fn();
    const onReturnToToday = vi.fn();
    render(
      createElement(JournalViewChrome, {
        day: JULY11_INITIAL_MODEL.day,
        access: JULY11_INITIAL_MODEL.access,
        quality: JULY11_INITIAL_MODEL.quality,
        source: JULY11_INITIAL_MODEL.source,
        onNavigateDay,
        onReturnToToday,
      }),
    );

    await user.click(screen.getByRole("button", { name: "Open previous Journal day" }));
    await user.click(screen.getByRole("button", { name: "Open next Journal day" }));
    await user.click(screen.getByRole("button", { name: "Today" }));

    expect(onNavigateDay).toHaveBeenNthCalledWith(1, "previous");
    expect(onNavigateDay).toHaveBeenNthCalledWith(2, "next");
    expect(onReturnToToday).toHaveBeenCalledOnce();
  });

  it("renders read-only and offline truthfulness as separate notices", () => {
    const readOnly = JOURNAL_READ_ONLY_FIXTURE.model;
    const offline = JOURNAL_OFFLINE_FIXTURE.model;
    const { rerender } = render(
      createElement(JournalViewChrome, {
        day: readOnly.day,
        access: readOnly.access,
        quality: readOnly.quality,
        source: readOnly.source,
      }),
    );

    expect(screen.getByText("Read only.")).toBeInTheDocument();
    expect(screen.queryByText("Offline.")).not.toBeInTheDocument();

    rerender(
      createElement(JournalViewChrome, {
        day: offline.day,
        access: offline.access,
        quality: offline.quality,
        source: offline.source,
      }),
    );
    expect(screen.getByText("Read only.")).toBeInTheDocument();
    expect(screen.getByText("Offline.")).toBeInTheDocument();
  });
});
