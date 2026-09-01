import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  asWidgetInstanceId,
  type WidgetPresentationContext,
} from "../../dashboard/contributions/contracts";
import type {
  CanvasThemeSnapshot,
  ResolvedThemeSummary,
} from "../../dashboard/contributions/themeContract";
import { JOURNAL_VIEW_DEFINITION_ID } from "./bindings";
import type { JournalGenericModuleInput } from "./contracts";
import JournalGenericModule from "./JournalGenericModule";

const useDocumentSession = vi.hoisted(() => vi.fn((options: {
  storeId: string;
  documentId: string;
  readOnly?: boolean;
  includeTruthProjection?: boolean;
}) => ({
  key: JSON.stringify([options.storeId, options.documentId]),
  reference: {
    kind: "workspace" as const,
    storeId: options.storeId,
    documentId: options.documentId,
  },
  bridge: {},
  writable: options.readOnly !== true,
  syncStatus: "clean" as const,
})));

vi.mock("../cowork/session/DocumentSession", () => ({ useDocumentSession }));
vi.mock("../cowork/surface/DocumentWorkspacePanel", () => ({
  DocumentWorkspacePanel: (props: {
    reference: { binding: { bindingId: string; projectionMode: string } };
    primary: ReactNode;
    title: string;
    canOpenFull: boolean;
  }) => (
    <div
      data-testid="shared-document-panel"
      data-binding={props.reference.binding.bindingId}
      data-projection={props.reference.binding.projectionMode}
      data-open-full={String(props.canOpenFull)}
    >
      <h2>{props.title}</h2>
      {props.primary}
    </div>
  ),
}));

const theme = {
  contractVersion: 1,
  preference: { scheme: "dark", skinId: "wb.default" },
  resolvedScheme: "dark",
  skin: { id: "wb.default", version: 2, publisherAppId: "wb.core" },
  accessibility: { forcedColors: false, reducedMotion: false, reducedTransparency: false },
} satisfies ResolvedThemeSummary;

const canvas = {
  surfaceCanvas: "#000", surfaceRaised: "#111", textPrimary: "#fff",
  textSecondary: "#ccc", borderDefault: "#333", focusRing: "#58a6ff",
  dataSeries: ["#58a6ff"],
} satisfies CanvasThemeSnapshot;

const presentation = {
  instanceId: asWidgetInstanceId("custom.reflection"),
  viewId: JOURNAL_VIEW_DEFINITION_ID,
  width: 700, height: 500, sizeMode: "standard", interactionMode: "operate",
  editing: false, theme, getCanvasTheme: () => canvas,
} satisfies WidgetPresentationContext;

const availableInput = {
  instanceId: "custom.reflection",
  revision: "journal:document:one",
  dayId: "journal-day:2026-08-27:America/New_York:05:00",
  localDate: "2026-08-27",
  access: { mode: "read_write" },
  moduleTypeId: "document",
  moduleInstanceVersion: 2,
  moduleDefinitionVersion: 1,
  behaviorId: "provenance_only",
  behaviorVersion: 1,
  aiContribution: "allowed",
  label: "Daily reflection",
  fields: [],
  document: {
    state: "available",
    role: "daily_reflection",
    truthEligibility: "allowed",
    truthStartsDisabled: true,
  },
} satisfies JournalGenericModuleInput;

const current = {
  state: "current" as const,
  role: "daily_reflection",
  truthEligibility: "allowed" as const,
  truthStartsDisabled: true as const,
  href: "/app/cowork?store_id=store-one&document_id=doc-one",
  storeId: "store-one",
  documentId: "doc-one",
  bindingId: "binding-one",
  domainEntityId: "entity-one",
  contentAuthorityEpoch: 1,
  canOpenFull: true,
};

describe("Journal document module", () => {
  it("provisions on explicit intent and opens the shared Co-work panel session", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => ({
      intent_id: intent.intent_id,
      client_mutation_id: intent.client_mutation_id,
      status: "accepted" as const,
      value: current,
    }));
    render(
      <JournalGenericModule
        input={availableInput}
        emit={emit}
        presentation={presentation}
      />,
    );

    expect(screen.getByText(/Truth starts disabled/u)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create document" }));

    await waitFor(() => expect(screen.getByTestId("shared-document-panel")).toBeInTheDocument());
    expect(emit).toHaveBeenCalledWith(expect.objectContaining({
      intent_type: "wb.journal.document.open",
      instance_id: "custom.reflection",
      payload: {
        local_date: "2026-08-27",
        module_instance_version: 2,
      },
    }));
    expect(useDocumentSession).toHaveBeenCalledWith({
      storeId: "store-one",
      documentId: "doc-one",
      readOnly: false,
      includeTruthProjection: false,
    });
    expect(screen.getByTestId("shared-document-panel")).toHaveAttribute(
      "data-binding",
      "binding-one",
    );
    expect(screen.getByTestId("shared-document-panel")).toHaveAttribute(
      "data-projection",
      "none",
    );
  });

  it("reopens an existing day binding without provisioning another document", async () => {
    const user = userEvent.setup();
    const emit = vi.fn();
    render(
      <JournalGenericModule
        input={{ ...availableInput, document: current }}
        emit={emit}
        presentation={presentation}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Open document" }));
    expect(await screen.findByTestId("shared-document-panel")).toBeInTheDocument();
    expect(emit).not.toHaveBeenCalled();
  });
});
