import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ViewSnapshot } from "../../dashboard/contributions/contracts";
import type { ViewLocationAdapter } from "../../dashboard/contributions/viewModules";
import { createRuntime } from "./viewRuntime";

const location: ViewLocationAdapter = {
  getSearch: () => "",
  pushSearch: () => undefined,
  replaceSearch: () => undefined,
  subscribe: () => () => undefined,
};

describe("Tasks view chrome", () => {
  it("shows one plain-language editing notice at view level", () => {
    const reason = "Task editing is temporarily unavailable while setup finishes.";
    const runtime = createRuntime({ search: "", storage: window.localStorage, location });
    const snapshot = {
      model: {
        access: { mode: "read_only", reason },
        facets: { counts: { active: 5, focused: 1, inbox: 2 } },
        tasks: [],
      },
    } as unknown as ViewSnapshot;

    render(runtime.renderChrome!(snapshot, {}));

    expect(screen.getAllByText(reason)).toHaveLength(1);
    expect(screen.queryByText(/authority/i)).not.toBeInTheDocument();
    expect(screen.getByText("Tasks · Co-work knowledge")).toBeInTheDocument();
  });
});
