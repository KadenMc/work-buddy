import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  CoworkDocumentOriginNotice,
  parseCoworkDocumentChangeInspection,
} from "./CoworkDocumentOriginNotice";

const payload = {
  schema: "wb.cowork-document-change-inspection/v1",
  store_id: "store-1",
  document_id: "document-1",
  change_id: "a".repeat(32),
  operation_kind: "source_markdown_insert",
  committed_at: "2026-08-10T00:00:00.000Z",
  actors: {
    selected_by: JSON.stringify({ schema: "wb.actor-ref/v1", kind: "human" }),
    applied_by: JSON.stringify({ schema: "wb.actor-ref/v1", kind: "service" }),
  },
  assurance: {
    exact_copied_text: "document_kernel_verified",
    persistence: "persistence_verified",
  },
  source: {
    source_ref: "wb-source://authority/item",
    source_role: "human_input",
    originating_surface: "journal",
    provider_id: null,
    lifecycle_state: "active",
    copy_relation: "exact_copy",
  },
  binding: {
    binding_id: "b".repeat(32),
    domain_namespace: "journal",
    domain_kind: "running_note",
    domain_entity_id: "c".repeat(32),
    role: "running_note",
    content_authority: "co_work",
    content_authority_epoch: 1,
    lifecycle: "current",
  },
  heads: {
    base_structured_head_sha256: "d".repeat(64),
    result_structured_head_sha256: "e".repeat(64),
  },
};

describe("CoworkDocumentOriginNotice", () => {
  it("fails closed on an incomplete inspection payload", () => {
    expect(
      parseCoworkDocumentChangeInspection({ ...payload, source: { source_ref: "x" } }),
    ).toBeNull();
  });

  it("keeps the origin compact until the user asks for its details", async () => {
    const fetcher = vi.fn(async () =>
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    render(
      <CoworkDocumentOriginNotice
        storeId="store-1"
        documentId="document-1"
        changeId={"a".repeat(32)}
        fetcher={fetcher as typeof fetch}
      />,
    );

    expect(await screen.findByText("From a Running Note")).toBeInTheDocument();
    expect(screen.getByText("Exact source copy")).toBeInTheDocument();
    expect(screen.queryByText("Persistence")).not.toBeVisible();

    await userEvent.click(screen.getByText("From a Running Note"));

    expect(screen.getByText("journal")).toBeVisible();
    expect(screen.getByText("Human")).toBeVisible();
    expect(screen.getByText("Work Buddy")).toBeVisible();
    expect(screen.getByText("Verified")).toBeVisible();
    expect(fetcher).toHaveBeenCalledWith(
      `/api/truth/doc/document-1/changes/${"a".repeat(32)}?store_id=store-1`,
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });
});
