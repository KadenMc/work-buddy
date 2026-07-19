/**
 * Dashboard-citizenship proof (PRD I18): the Co-work surface clears axe in both
 * the light and the dark scheme. The scheme is forced through the shared
 * ThemeProvider (not a private toggle), which stamps `data-wb-scheme` on the root
 * exactly as production does, so the same tree the dashboard mounts is the tree
 * under test. Coverage here spans the composed surface (health strip, editor
 * pane, review rail) and the Chat tab.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { DashboardEventProvider } from "../../../dashboard/events/DashboardEventProvider";
import { ThemeProvider } from "../../../theme/ThemeProvider";
import type { ThemePreference } from "../../../theme/contracts";
import { expectNoAccessibilityViolations } from "../../../test/setup";
import { CoworkRail } from "../rail";
import { InMemoryReviewProvider } from "../rail";
import { createDemoChatProvider } from "../rail";
import { InMemoryCoworkProvider } from "../providers/InMemoryCoworkProvider";
import { COWORK_VIEW_DEFINITION } from "../viewDefinition";
import { CoworkWorkspaceSurface } from "../surface/CoworkWorkspaceSurface";

const SCHEMES: readonly ThemePreference[] = [
  { scheme: "light", skinId: "wb.default" },
  { scheme: "dark", skinId: "wb.default" },
];

const S1_TLDR = "Add the vault content hash to the cache key.";

function renderSurface(preference: ThemePreference) {
  return render(
    <ThemeProvider initialPreference={preference}>
      <DashboardEventProvider>
        <CoworkWorkspaceSurface
          definition={COWORK_VIEW_DEFINITION}
          provider={new InMemoryCoworkProvider()}
        />
      </DashboardEventProvider>
    </ThemeProvider>,
  );
}

function renderRail(preference: ThemePreference) {
  return render(
    <ThemeProvider initialPreference={preference}>
      <CoworkRail
        documentId="demo-doc"
        reviewProvider={new InMemoryReviewProvider()}
        chatProvider={createDemoChatProvider("conv-theme")}
        conversationId="conv-theme"
      />
    </ThemeProvider>,
  );
}

describe("Co-work theme matrix", () => {
  beforeEach(() => {
    localStorage.clear();
  });
  afterEach(() => {
    localStorage.clear();
  });

  for (const preference of SCHEMES) {
    it(`clears axe on the composed surface in the ${preference.scheme} scheme`, async () => {
      const { container } = renderSurface(preference);
      await waitFor(
        () => expect(screen.getByText("Co-work demo document")).toBeVisible(),
        { timeout: 10_000 },
      );
      await waitFor(
        () => expect(container.querySelector(".ProseMirror")).not.toBeNull(),
        { timeout: 10_000 },
      );
      expect(document.documentElement.dataset.wbScheme).toBe(preference.scheme);
      await expectNoAccessibilityViolations(container);
    }, 15_000);

    it(`clears axe on the Chat tab in the ${preference.scheme} scheme`, async () => {
      const { container } = renderRail(preference);
      await waitFor(() => expect(screen.getByText(S1_TLDR)).toBeVisible());
      await userEvent.click(screen.getByRole("tab", { name: /Chat/ }));
      await waitFor(() =>
        expect(
          screen.getByText(/I proposed a few tracked edits/),
        ).toBeVisible(),
      );
      expect(document.documentElement.dataset.wbScheme).toBe(preference.scheme);
      await expectNoAccessibilityViolations(container);
    });
  }
});
