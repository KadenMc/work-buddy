import { expect, test } from "@playwright/test";

const SETTING_ID = "wb.embedding.document-execution";

test("Embedding settings distinguish saved policy from the running route", { tag: "@ci" }, async ({
  page,
}) => {
  let configuredPolicy = "local";
  let revision = "value:0";

  await page.route("**/api/settings/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname === "/api/settings/registry") {
      await route.fulfill({
        json: {
          schema_version: 1,
          registry_revision: "settings-registry:e2e",
          definitions: [{
            setting_id: SETTING_ID,
            definition_version: 1,
            value_version: 1,
            owner: { kind: "system", id: "wb.embedding", label: "Embeddings" },
            provenance: {
              complement_id: "wb.embedding",
              complement_version: "e2e",
              trust_tier: "native",
              label: "Embedding service",
            },
            title: "Document embedding execution",
            short_description: "Choose where the large passage encoder runs for document similarity and indexing.",
            long_description: "Query encoders stay local.",
            default_value: "local",
            allowed_scopes: ["profile"],
            default_scope: "profile",
            applies_to: [{ kind: "system", id: "wb.embedding", label: "Embeddings" }],
            presentation: {
              control: "select",
              apply_behavior: "restart-component",
              options: [
                { value: "local", label: "Local only", description: "Always encode locally." },
                { value: "prefer-lmstudio", label: "Prefer LM Studio", description: "Fall back locally." },
                { value: "require-lmstudio", label: "Require LM Studio", description: "Never load locally." },
              ],
            },
            visibility: "frontend",
            sensitivity: "ordinary",
          }],
          pages: [{
            page_id: "wb.settings.system.embeddings",
            context: { kind: "system", id: "wb.embedding", label: "Embeddings" },
            owner: { kind: "system", id: "wb.embedding", label: "Embeddings" },
            route: "/app/settings/system/embeddings",
            label: "Embeddings",
            description: "Control document-encoding placement and inspect the active route.",
            navigation_group: "system",
            navigation_label: "Embeddings",
            navigation_order: 20,
            sections: [{ section_id: "execution", label: "Execution" }],
          }],
          placements: [{
            placement_id: "wb.settings.placement.system.embeddings.document-execution",
            setting_id: SETTING_ID,
            page_id: "wb.settings.system.embeddings",
            section_id: "execution",
          }],
        },
      });
      return;
    }

    if (url.pathname === "/api/settings/values" && request.method() === "GET") {
      const pending = configuredPolicy === "local" ? null : configuredPolicy;
      await route.fulfill({
        json: {
          schema_version: 1,
          registry_revision: "settings-registry:e2e",
          observed_at: "2026-09-08T12:00:00Z",
          read_only: false,
          diagnostics: [],
          values: [{
            setting_id: SETTING_ID,
            scope: { kind: "profile", subject_id: "default" },
            effective_value: "local",
            configured_value: configuredPolicy,
            pending_value: pending,
            source: configuredPolicy === "local" ? "default" : "profile",
            is_modified: configuredPolicy !== "local",
            revision,
            apply_status: pending ? "restart-required" : "effective",
            diagnostics: [],
          }],
        },
      });
      return;
    }

    if (
      url.pathname === `/api/settings/values/${SETTING_ID}` &&
      request.method() === "PATCH"
    ) {
      const body = request.postDataJSON() as {
        value: string;
        expected_revision: string;
      };
      expect(body).toEqual(expect.objectContaining({
        value: "require-lmstudio",
        expected_revision: "value:0",
      }));
      configuredPolicy = body.value;
      revision = "value:1";
      await route.fulfill({
        json: {
          schema_version: 1,
          registry_revision: "settings-registry:e2e",
          value: {
            setting_id: SETTING_ID,
            scope: { kind: "profile", subject_id: "default" },
            effective_value: "local",
            configured_value: configuredPolicy,
            pending_value: configuredPolicy,
            source: "profile",
            is_modified: true,
            revision,
            apply_status: "restart-required",
            diagnostics: [],
          },
        },
      });
      return;
    }

    await route.fulfill({ status: 404 });
  });

  await page.route("**/api/embeddings/runtime", async (route) => {
    await route.fulfill({
      json: {
        service_status: "ok",
        policy: {
          effective: "local",
          configured: configuredPolicy,
          pending: configuredPolicy === "local" ? null : configuredPolicy,
          apply_status: configuredPolicy === "local" ? "effective" : "restart-required",
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
      },
    });
  });

  await page.goto("/app/settings/system/embeddings");

  await expect(page.getByRole("heading", { name: "Embeddings", level: 1 }))
    .toBeVisible();
  await expect(page.getByRole("heading", { name: "Active document route" }))
    .toBeVisible();
  await expect(page.getByText("Not loaded", { exact: true })).toBeVisible();
  await expect(page.getByText("Not checked (local-only policy)")).toBeVisible();
  await expect(page.getByText("~526 MB weights not loaded yet")).toBeVisible();
  await expect(page.getByText(/not a guaranteed change in process private commit/))
    .toBeVisible();

  const selector = page.getByRole("combobox", {
    name: "Document embedding execution",
  });
  await expect(selector).toBeEnabled();
  await selector.selectOption("require-lmstudio");
  await page.getByRole("button", { name: "Save change" }).click();

  await expect(page.getByText(
    "Saved. Restart the Embedding service to apply this choice.",
  )).toBeVisible();
  await expect(page.getByRole("status").filter({
    hasText: "The effective setting remains Local only",
  })).toContainText("Restart Embedding service to activate Require LM Studio");

  await page.getByRole("button", { name: "Refresh status" }).click();
  await expect(page.getByText(
    "Require LM Studio is saved, but the running service remains on Local only until restart.",
  )).toBeVisible();
  await expect(page.getByText("Local only", { exact: true }).last()).toBeVisible();
});
