import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TypographyScaleProvider } from "../theme/TypographyScaleProvider";
import { SettingsPage } from "./SettingsPage";
import { SettingsRegistry } from "./registry";
import {
  asSettingId,
  asSettingPlacementId,
  type SettingsContribution,
} from "./contracts";
import { EMBEDDING_SETTINGS_PAGE_ID } from "./EmbeddingRuntimePanel";

const settingId = asSettingId("wb.embedding.document-execution");

const contribution: SettingsContribution = {
  sourceId: "test.embedding-settings",
  definitions: [{
    schemaVersion: 1,
    settingId,
    definitionVersion: 1,
    valueVersion: 1,
    ownerId: "wb.embedding",
    ownerLabel: "Embeddings",
    provenance: {
      complementId: "wb.embedding",
      complementVersion: "test",
      trustTier: "native",
      label: "Embedding service",
    },
    title: "Document embedding execution",
    summary: "Choose where the large passage encoder runs for document similarity and indexing.",
    details: "Query encoders stay local.",
    defaultValue: "local",
    allowedScopes: ["profile"],
    defaultScope: "profile",
    control: {
      kind: "select",
      options: [
        { value: "local", label: "Local only", description: "Always encode locally." },
        { value: "prefer-lmstudio", label: "Prefer LM Studio", description: "Fall back locally." },
        { value: "require-lmstudio", label: "Require LM Studio", description: "Never load locally." },
      ],
    },
    appliesTo: [{ kind: "system", id: "wb.embedding", label: "Embeddings" }],
    applyBehavior: "restart-component",
    sensitivity: "ordinary",
    visibility: "frontend",
  }],
  pages: [{
    schemaVersion: 1,
    pageId: EMBEDDING_SETTINGS_PAGE_ID,
    ownerId: "wb.embedding",
    route: "/settings/system/embeddings",
    label: "Embeddings",
    description: "Control document-encoding placement and inspect the active route.",
    navigationGroup: "system",
    navigationLabel: "Embeddings",
    navigationOrder: 20,
    context: { kind: "system", id: "wb.embedding", label: "Embeddings" },
    sections: [{ sectionId: "execution", label: "Execution", order: 10 }],
  }],
  placements: [{
    schemaVersion: 1,
    placementId: asSettingPlacementId("test.embedding.placement"),
    settingId,
    pageId: EMBEDDING_SETTINGS_PAGE_ID,
    sectionId: "execution",
    order: 10,
  }],
};

const registry = new SettingsRegistry([contribution]);

function value(overrides: Record<string, unknown> = {}) {
  return {
    setting_id: settingId,
    scope: { kind: "profile", subject_id: "default" },
    effective_value: "local",
    configured_value: "local",
    source: "default",
    is_modified: false,
    revision: "value:0",
    pending_value: null,
    apply_status: "effective",
    diagnostics: [],
    ...overrides,
  };
}

describe("Embedding settings", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("saves a restart-gated route without claiming it is already active", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/settings/values?")) {
        return Response.json({
          schema_version: 1,
          registry_revision: "settings-registry:8",
          observed_at: "2026-09-08T12:00:00Z",
          read_only: false,
          diagnostics: [],
          values: [value()],
        });
      }
      if (url === "/api/embeddings/runtime") {
        return Response.json({
          service_status: "ok",
          policy: {
            effective: "local",
            configured: "local",
            pending: null,
            apply_status: "effective",
            running: "local",
            authority_matches_runtime: true,
          },
          document_model: {
            loaded_locally: false,
            routing: {
              requested_provider: "local",
              provider_model: "leaf-ir",
              on_error: "fallback",
              cooldown_remaining_s: 0,
              last_route: null,
            },
          },
          lmstudio: null,
        });
      }
      if (url === `/api/settings/values/${settingId}` && init?.method === "PATCH") {
        expect(JSON.parse(String(init.body))).toMatchObject({
          value: "require-lmstudio",
          expected_revision: "value:0",
        });
        return Response.json({
          schema_version: 1,
          registry_revision: "settings-registry:8",
          value: value({
            configured_value: "require-lmstudio",
            pending_value: "require-lmstudio",
            is_modified: true,
            revision: "value:1",
            apply_status: "restart-required",
          }),
        });
      }
      return new Response(null, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter initialEntries={["/settings/system/embeddings"]}>
        <TypographyScaleProvider initialScale="standard">
          <SettingsPage registryOverride={registry} />
        </TypographyScaleProvider>
      </MemoryRouter>,
    );

    const select = await screen.findByRole("combobox", {
      name: "Document embedding execution",
    });
    await waitFor(() => expect(select).toBeEnabled());
    fireEvent.change(select, { target: { value: "require-lmstudio" } });
    const saveButton = screen.getByRole("button", { name: "Save change" });
    expect(saveButton).toBeEnabled();
    fireEvent.click(saveButton);

    expect(await screen.findByText(
      "Saved. Restart the Embedding service to apply this choice.",
    )).toBeInTheDocument();
    const restartStatus = screen.getByText((_, element) =>
      element?.getAttribute("role") === "status" &&
      element.textContent?.includes("effective setting remains Local only") === true,
    );
    expect(restartStatus).toHaveTextContent("Restart Embedding service to activate Require LM Studio");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/embeddings/runtime",
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    ));
  });
});
