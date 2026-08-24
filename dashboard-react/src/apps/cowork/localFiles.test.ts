import { beforeEach, describe, expect, it, vi } from "vitest";

import { resetLocalIdentityForTests } from "../../security/localIdentity";
import {
  isAllowedCoworkLinkUri,
  parseCoworkLocalFileHref,
} from "./document-kernel/schema";
import { createCoworkMarkdownManager } from "./editor/extensions";
import {
  HttpCoworkLocalFileClient,
  type CoworkLocalFileLink,
} from "./localFiles";
import { authenticatedHumanAuthorityFetch } from "./testSupport/authenticatedHumanAuthorityFetch";

const STORE_ID = "a".repeat(32);
const DOCUMENT_ID = "b".repeat(32);
const LINK_ID = `pdf_${"c".repeat(28)}`;

const linkedPdf: CoworkLocalFileLink = {
  linkId: LINK_ID,
  href: `wb-local-file:${LINK_ID}`,
  displayName: "Reference PDF",
  suffix: ".pdf",
  mediaType: "application/pdf",
  byteLength: 1234,
  sensitivity: "ordinary",
  allowedAction: "open",
  availability: "verified",
  localActionAvailable: true,
};

beforeEach(() => resetLocalIdentityForTests());

describe("Co-work Link URI policy", () => {
  it("admits exact opaque local links and rejects paths or active schemes", () => {
    expect(isAllowedCoworkLinkUri(`wb-local-file:${LINK_ID}`)).toBe(true);
    expect(parseCoworkLocalFileHref(`wb-local-file:${LINK_ID}`)).toBe(LINK_ID);
    expect(isAllowedCoworkLinkUri("https://example.test/reference")).toBe(true);
    expect(isAllowedCoworkLinkUri("mailto:user@example.test")).toBe(true);
    expect(isAllowedCoworkLinkUri("wb-truth://store/claim/claim-1")).toBe(true);
    expect(isAllowedCoworkLinkUri("file:///C:/secret.ppk")).toBe(false);
    expect(isAllowedCoworkLinkUri("javascript:alert(1)")).toBe(false);
    expect(isAllowedCoworkLinkUri("data:text/plain,secret")).toBe(false);
    expect(isAllowedCoworkLinkUri(`wb-local-file://${LINK_ID}`)).toBe(false);
    expect(isAllowedCoworkLinkUri(`wb-local-file:${LINK_ID}?download=1`)).toBe(false);
    expect(isAllowedCoworkLinkUri(`wb-local-file:${LINK_ID}#fragment`)).toBe(false);
    expect(isAllowedCoworkLinkUri("wb-local-file:../../secret.ppk")).toBe(false);
  });

  it("keeps disallowed Markdown links inert across structured round-trip", () => {
    const manager = createCoworkMarkdownManager();
    const parsed = manager.parse(
      `[unsafe](file:///C:/secret.ppk) [local](wb-local-file:${LINK_ID})`,
    );
    const hrefs: string[] = [];
    const visit = (value: unknown): void => {
      if (Array.isArray(value)) {
        value.forEach(visit);
        return;
      }
      if (typeof value !== "object" || value === null) return;
      const row = value as Record<string, unknown>;
      const attrs = row.attrs;
      if (typeof attrs === "object" && attrs !== null) {
        const href = (attrs as Record<string, unknown>).href;
        if (typeof href === "string") hrefs.push(href);
      }
      Object.values(row).forEach(visit);
    };
    visit(parsed);
    expect(hrefs).toContain(`wb-local-file:${LINK_ID}`);
    expect(hrefs).not.toContain("file:///C:/secret.ppk");
    const roundTrip = manager.serialize(parsed);
    expect(roundTrip).not.toContain("file:///C:/secret.ppk");
  });
});

describe("HttpCoworkLocalFileClient", () => {
  it("reads metadata and binds activation to exact human authority and intent", async () => {
    const applicationCalls: Array<{
      readonly url: string;
      readonly init?: RequestInit;
    }> = [];
    const fetchImpl = authenticatedHumanAuthorityFetch(async (input, init) => {
      const url = String(input);
      applicationCalls.push({ url, init });
      if (init?.method === "GET") {
        return new Response(
          JSON.stringify({
            ok: true,
            links: [
              {
                link_id: LINK_ID,
                href: `wb-local-file:${LINK_ID}`,
                display_name: "Reference PDF",
                suffix: ".pdf",
                media_type: "application/pdf",
                byte_length: 1234,
                sensitivity: "ordinary",
                allowed_action: "open",
                availability: "verified",
                local_action_available: true,
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ ok: true, status: "opened" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    const client = new HttpCoworkLocalFileClient({
      storeId: STORE_ID,
      documentId: DOCUMENT_ID,
      fetchImpl,
    });

    const links = await client.list();
    expect(links).toEqual([linkedPdf]);
    await client.activate(links[0]);

    expect(applicationCalls[0]?.url).toBe(
      `/api/truth/doc/${DOCUMENT_ID}/local-files?store_id=${STORE_ID}`,
    );
    const activation = applicationCalls[1];
    expect(activation?.url).toBe(
      `/api/truth/doc/${DOCUMENT_ID}/local-files/${LINK_ID}/activate?store_id=${STORE_ID}`,
    );
    expect(activation?.init?.method).toBe("POST");
    expect(activation?.init?.headers).toMatchObject({
      "Content-Type": "application/json",
      "X-Work-Buddy-Intent": "cowork-local-file-open",
      "X-WB-CSRF": "test-csrf-token",
      "X-WB-Gesture": "test-gesture-1",
    });
    expect(JSON.parse(String(activation?.init?.body))).toEqual({
      link_id: LINK_ID,
      action: "open",
    });
  });

  it("fails closed when metadata contains a path-shaped href", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      new Response(
        JSON.stringify({
          ok: true,
          links: [
            {
              link_id: LINK_ID,
              href: "file:///C:/secret.ppk",
              display_name: "Secret",
              suffix: ".ppk",
              media_type: "application/x-putty-private-key",
              byte_length: 42,
              sensitivity: "credential",
              allowed_action: "reveal",
              availability: "verified",
              local_action_available: true,
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    const client = new HttpCoworkLocalFileClient({
      storeId: STORE_ID,
      documentId: DOCUMENT_ID,
      fetchImpl,
    });
    await expect(client.list()).rejects.toThrow(
      "invalid linked-file catalog",
    );
  });
});
