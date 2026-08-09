import { describe, expect, it } from "vitest";

import { coworkErrorMessage, normalizeCoworkError } from "./errors";

describe("normalizeCoworkError", () => {
  it("keeps a legacy string error as the human-readable message", () => {
    expect(
      normalizeCoworkError(
        { error: "That folder is not reachable by Co-work." },
        404,
      ),
    ).toMatchObject({
      code: "not_found",
      message: "That folder is not reachable by Co-work.",
      retryable: false,
      status: 404,
    });
  });

  it("still treats a code-shaped legacy error as the error code", () => {
    expect(
      normalizeCoworkError(
        { error: "folder_unreachable" },
        503,
        "Co-work couldn’t load the folder.",
      ),
    ).toMatchObject({
      code: "folder_unreachable",
      message: "Co-work couldn’t load the folder.",
      retryable: true,
      status: 503,
    });
  });

  it("also accepts a bare legacy error string", () => {
    expect(
      normalizeCoworkError("The folder could not be opened.", 500),
    ).toMatchObject({
      code: "request_failed",
      message: "The folder could not be opened.",
      retryable: true,
      status: 500,
    });
  });
});

describe("coworkErrorMessage", () => {
  it("keeps useful human guidance", () => {
    expect(
      coworkErrorMessage(
        { message: "Markdown changed outside Co-work; review it before saving." },
        "The file couldn’t be saved.",
      ),
    ).toBe("Markdown changed outside Co-work; review it before saving.");
  });

  it("keeps storage internals in diagnostics instead of UI copy", () => {
    expect(
      coworkErrorMessage(
        { message: "Canonical Y.Doc snapshot SHA256 did not match the structured head." },
        "This document couldn’t be opened.",
      ),
    ).toBe("This document couldn’t be opened.");
  });
});
