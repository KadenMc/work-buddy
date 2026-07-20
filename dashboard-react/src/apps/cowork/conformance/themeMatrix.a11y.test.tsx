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
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  asViewId,
  asWidgetInstanceId,
  type WidgetPresentationContext,
} from "../../../dashboard/contributions/contracts";
import { DashboardEventProvider } from "../../../dashboard/events/DashboardEventProvider";
import { ThemeProvider } from "../../../theme/ThemeProvider";
import type { ThemePreference } from "../../../theme/contracts";
import { fallbackCanvasTheme } from "../../../theme/resolveTheme";
import { expectNoAccessibilityViolations } from "../../../test/setup";
import { CoworkRail } from "../rail";
import { InMemoryReviewProvider } from "../rail";
import { createDemoChatProvider } from "../rail";
import type { CoworkDocumentSummary, CoworkWorkspaceInput } from "../contracts";
import CoworkWorkspaceWidget from "../widget/CoworkWorkspaceWidget";

const SCHEMES: readonly ThemePreference[] = [
  { scheme: "light", skinId: "wb.default" },
  { scheme: "dark", skinId: "wb.default" },
];

const S1_TLDR = "Add the vault content hash to the cache key.";

const DEMO_DOCUMENT: CoworkDocumentSummary = {
  documentId: "demo-doc",
  path: "docs/demo/co-work-demo.md",
  title: "Co-work demo document",
  profile: "co_authored",
  driftState: "clean",
  openProposalCount: 0,
  openFlagCount: 0,
};

const DEMO_INPUT: CoworkWorkspaceInput = {
  document: DEMO_DOCUMENT,
  sessionQuality: "demo",
};

const presentation: WidgetPresentationContext = {
  instanceId: asWidgetInstanceId("wb-cowork:workspace"),
  viewId: asViewId("wb.cowork.workspace"),
  width: 1280,
  height: 720,
  sizeMode: "expanded",
  interactionMode: "operate",
  editing: false,
  theme: {
    contractVersion: 1,
    preference: { scheme: "light", skinId: "wb.default" },
    resolvedScheme: "light",
    skin: { id: "wb.default", version: 2, publisherAppId: "wb.core" },
    accessibility: {
      forcedColors: false,
      reducedMotion: false,
      reducedTransparency: false,
    },
  },
  getCanvasTheme: () => fallbackCanvasTheme("light"),
};

const noopEmit: ComponentProps<typeof CoworkWorkspaceWidget>["emit"] = async (
  intent,
) => ({ intent_id: intent.intent_id, status: "accepted" });

/**
 * The workspace card is a grid widget now, so the theme matrix drives its renderer with the
 * demo input under the shared ThemeProvider. The single `<main>` stands in for the grid host
 * that owns the one page landmark, matching how the WidgetFrame wraps the card in
 * production. The scheme is still forced through the ThemeProvider, so the same themed tree
 * the dashboard mounts is the tree under axe.
 */
function renderSurface(preference: ThemePreference) {
  return render(
    <ThemeProvider initialPreference={preference}>
      <DashboardEventProvider>
        <main>
          <CoworkWorkspaceWidget
            input={DEMO_INPUT}
            emit={noopEmit}
            presentation={presentation}
          />
        </main>
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
  const originalUrl = window.location.href;
  beforeEach(() => {
    localStorage.clear();
    // Drive the fabricated demo scene so axe covers the populated composed surface
    // (health strip, seeded editor, and review rail with cards), not the empty default.
    window.history.replaceState({}, "", "/app/cowork?cowork_fixture=demo");
  });
  afterEach(() => {
    localStorage.clear();
    window.history.replaceState({}, "", originalUrl);
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
