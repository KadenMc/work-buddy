import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { CAPTURE_APP_CONTRIBUTION } from "../../widget-library/capture/contribution";
import {
  asViewId,
  asWidgetInstanceId,
} from "../contributions/contracts";
import { InMemoryWidgetDraftRepository } from "./repository";
import type { WidgetDraftRepository } from "./contracts";
import {
  WidgetDraftRuntimeProvider,
  WidgetDraftScopeProvider,
  useWidgetDraft,
} from "./WidgetDraftRuntime";

const definition = CAPTURE_APP_CONTRIBUTION.widgetDefinitions[0];
const viewId = asViewId("wb.journal.main");
const instanceId = asWidgetInstanceId("journal:capture");

function DraftEditor() {
  const draft = useWidgetDraft(
    "capture",
    { text: "", targetId: "log", mode: "dumb" },
    { isPristine: (value) => value.text.length === 0 },
  );
  if (!draft.ready) return <p>Loading draft</p>;
  return (
    <>
      <label>
        Draft
        <input
          aria-label="Draft"
          value={draft.value.text}
          onChange={(event) =>
            draft.setValue((current) => ({ ...current, text: event.target.value }))
          }
        />
      </label>
      <output aria-label="Draft status">{draft.status}</output>
      <output aria-label="Draft revision">{draft.revision}</output>
    </>
  );
}

function Harness({
  repository,
  dayId = "2026-07-11",
}: {
  readonly repository: WidgetDraftRepository;
  readonly dayId?: string;
}) {
  return (
    <WidgetDraftRuntimeProvider repository={repository}>
      <WidgetDraftScopeProvider
        definition={definition}
        viewId={viewId}
        instanceId={instanceId}
        input={{ dayId }}
      >
        <DraftEditor />
      </WidgetDraftScopeProvider>
    </WidgetDraftRuntimeProvider>
  );
}

function FailingEditor() {
  const draft = useWidgetDraft("capture", { text: "", targetId: "log", mode: "dumb" }, {
    isPristine: (value) => value.text.length === 0,
  });
  const [flushResult, setFlushResult] = useState("not-run");
  if (!draft.ready) return <p>Loading draft</p>;
  return (
    <>
      <button type="button" onClick={() => draft.setValue({ ...draft.value, text: "unsafe" })}>
        Change
      </button>
      <button
        type="button"
        onClick={() => {
          void draft.flush().then(
            () => setFlushResult("resolved"),
            () => setFlushResult("rejected"),
          );
        }}
      >
        Flush
      </button>
      <output>{draft.status}</output>
      <output>{flushResult}</output>
    </>
  );
}

describe("WidgetDraftRuntime", () => {
  it("restores the exact structured value after the renderer unmounts", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const first = render(<Harness repository={repository} />);
    const input = await screen.findByRole("textbox", { name: "Draft" });
    await userEvent.type(input, "  exact draft  ");
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Draft status" })).toHaveTextContent(
        "saved",
      ),
    );
    first.unmount();

    render(<Harness repository={repository} />);
    expect(await screen.findByRole("textbox", { name: "Draft" })).toHaveValue(
      "  exact draft  ",
    );
  });

  it("scopes the same widget instance independently by the declared input field", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const rendered = render(<Harness repository={repository} dayId="day-1" />);
    await userEvent.type(await screen.findByRole("textbox", { name: "Draft" }), "day one");
    await waitFor(() => expect(screen.getByText("saved")).toBeInTheDocument());

    rendered.rerender(<Harness repository={repository} dayId="day-2" />);
    expect(await screen.findByRole("textbox", { name: "Draft" })).toHaveValue("");

    rendered.rerender(<Harness repository={repository} dayId="day-1" />);
    expect(await screen.findByRole("textbox", { name: "Draft" })).toHaveValue("day one");
  });

  it("rejects flush when device persistence fails so callers cannot dispatch unsafely", async () => {
    const repository: WidgetDraftRepository = {
      load: async () => undefined,
      save: async () => {
        throw new Error("quota unavailable");
      },
      delete: async () => undefined,
    };
    render(
      <WidgetDraftRuntimeProvider repository={repository}>
        <WidgetDraftScopeProvider
          definition={definition}
          viewId={viewId}
          instanceId={instanceId}
          input={{ dayId: "day-1" }}
        >
          <FailingEditor />
        </WidgetDraftScopeProvider>
      </WidgetDraftRuntimeProvider>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Change" }));
    await waitFor(() => expect(screen.getByText("error")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Flush" }));
    expect(await screen.findByText("rejected")).toBeInTheDocument();
  });
});
