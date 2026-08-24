/** Metadata-only local-file links for registered Co-work documents. */

import { coworkHumanAuthorityHeaders } from "../../security/humanAuthority";
import { parseCoworkLocalFileHref } from "./document-kernel/schema";
import { CoworkHttpError, normalizeCoworkError } from "./providers/errors";

export type CoworkLocalFileAction = "open" | "reveal";
export type CoworkLocalFileAvailability =
  | "verified"
  | "changed"
  | "policy_changed"
  | "unavailable";

export interface CoworkLocalFileLink {
  readonly linkId: string;
  readonly href: string;
  readonly displayName: string;
  readonly suffix: ".pdf" | ".ppk";
  readonly mediaType: string;
  readonly byteLength: number;
  readonly sensitivity: string;
  readonly allowedAction: CoworkLocalFileAction;
  readonly availability: CoworkLocalFileAvailability;
  readonly localActionAvailable: boolean;
}

export interface CoworkLocalFileClient {
  list(options?: { readonly refresh?: boolean }): Promise<readonly CoworkLocalFileLink[]>;
  activate(link: CoworkLocalFileLink): Promise<void>;
}

interface HttpCoworkLocalFileClientOptions {
  readonly storeId: string;
  readonly documentId: string;
  readonly fetchImpl?: typeof fetch;
}

const objectValue = (value: unknown): Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};

const parseLink = (value: unknown): CoworkLocalFileLink | null => {
  const row = objectValue(value);
  const href = typeof row.href === "string" ? row.href : "";
  const linkId = parseCoworkLocalFileHref(href);
  const action = row.allowed_action;
  const suffix = row.suffix;
  const availability = row.availability;
  if (
    linkId === null ||
    row.link_id !== linkId ||
    typeof row.display_name !== "string" ||
    (suffix !== ".pdf" && suffix !== ".ppk") ||
    typeof row.media_type !== "string" ||
    typeof row.byte_length !== "number" ||
    !Number.isSafeInteger(row.byte_length) ||
    row.byte_length < 0 ||
    typeof row.sensitivity !== "string" ||
    (action !== "open" && action !== "reveal") ||
    (suffix === ".pdf" && action !== "open") ||
    (suffix === ".ppk" && action !== "reveal") ||
    ![
      "verified",
      "changed",
      "policy_changed",
      "unavailable",
    ].includes(String(availability)) ||
    typeof row.local_action_available !== "boolean"
  ) {
    return null;
  }
  return {
    linkId,
    href,
    displayName: row.display_name,
    suffix,
    mediaType: row.media_type,
    byteLength: row.byte_length,
    sensitivity: row.sensitivity,
    allowedAction: action,
    availability: availability as CoworkLocalFileAvailability,
    localActionAvailable: row.local_action_available,
  };
};

const actionIntent = (action: CoworkLocalFileAction): string =>
  action === "open" ? "cowork-local-file-open" : "cowork-local-file-reveal";

export class HttpCoworkLocalFileClient implements CoworkLocalFileClient {
  readonly #storeId: string;
  readonly #documentId: string;
  readonly #fetch: typeof fetch;

  constructor(options: HttpCoworkLocalFileClientOptions) {
    this.#storeId = options.storeId;
    this.#documentId = options.documentId;
    this.#fetch = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  #baseEndpoint(): string {
    return `/api/truth/doc/${encodeURIComponent(this.#documentId)}/local-files`;
  }

  async list(
    _options: { readonly refresh?: boolean } = {},
  ): Promise<readonly CoworkLocalFileLink[]> {
    const response = await this.#fetch(
      `${this.#baseEndpoint()}?store_id=${encodeURIComponent(this.#storeId)}`,
      { method: "GET", credentials: "same-origin", cache: "no-store" },
    );
    const payload = objectValue(await response.json().catch(() => ({})));
    if (!response.ok || payload.ok !== true || !Array.isArray(payload.links)) {
      throw new CoworkHttpError(
        normalizeCoworkError(
          payload,
          response.status,
          "Co-work couldn’t inspect the linked local files.",
        ),
      );
    }
    const links = payload.links.map(parseLink);
    if (links.some((link) => link === null)) {
      throw new Error("Co-work returned an invalid linked-file catalog.");
    }
    return links as readonly CoworkLocalFileLink[];
  }

  async activate(link: CoworkLocalFileLink): Promise<void> {
    if (
      parseCoworkLocalFileHref(link.href) !== link.linkId ||
      link.availability !== "verified" ||
      !link.localActionAvailable ||
      (link.suffix === ".ppk" && link.allowedAction !== "reveal") ||
      (link.suffix === ".pdf" && link.allowedAction !== "open")
    ) {
      throw new Error("This linked local file is not available for that action.");
    }
    const body = { link_id: link.linkId, action: link.allowedAction };
    const authorityHeaders = await coworkHumanAuthorityHeaders(
      {
        operation: `local_file.${link.allowedAction}`,
        storeId: this.#storeId,
        documentId: this.#documentId,
        body,
      },
      this.#fetch,
    );
    const response = await this.#fetch(
      `${this.#baseEndpoint()}/${encodeURIComponent(link.linkId)}/activate?store_id=${encodeURIComponent(this.#storeId)}`,
      {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-Work-Buddy-Intent": actionIntent(link.allowedAction),
          ...authorityHeaders,
        },
        body: JSON.stringify(body),
      },
    );
    const payload = objectValue(await response.json().catch(() => ({})));
    if (!response.ok || payload.ok !== true) {
      throw new CoworkHttpError(
        normalizeCoworkError(
          payload,
          response.status,
          "The linked local file could not be opened.",
        ),
      );
    }
  }
}

export const linkedLocalFileWarning = (link: CoworkLocalFileLink): string =>
  link.allowedAction === "reveal"
    ? "This is a credential-like file. Work Buddy will only reveal its location; it will not open, preview, copy, upload, or download the file. Continue?"
    : "";
