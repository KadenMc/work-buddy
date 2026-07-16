import { afterEach, describe, expect, it, vi } from "vitest";

import { systemStatusClient } from "./client";

afterEach(() => vi.unstubAllGlobals());

describe("systemStatusClient", () => {
  it("sends fixer inputs in the established params envelope", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({ ok: true, detail: "fixed" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await systemStatusClient.repair("req:calendar/token", { token: "reference" });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/control/fix/calendar%2Ftoken",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ params: { token: "reference" } }),
      }),
    );
  });

  it("does not announce an HTTP-200 fixer failure as success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({ ok: false, detail: "Still missing a token" }),
      ),
    );

    await expect(
      systemStatusClient.repair("req:calendar/token"),
    ).rejects.toThrow("Still missing a token");
  });
});
