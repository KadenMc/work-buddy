import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EmbeddingRuntimePanel } from "./EmbeddingRuntimePanel";

describe("EmbeddingRuntimePanel", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("makes a required-remote outage and no-local-fallback guarantee explicit", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({
      service_status: "ok",
      policy: {
        effective: "require-lmstudio",
        configured: "require-lmstudio",
        pending: null,
        apply_status: "effective",
        running: "require-lmstudio",
        authority_matches_runtime: true,
      },
      document_model: {
        loaded_locally: false,
        routing: {
          requested_provider: "lmstudio",
          provider_model: "remote-leaf-ir",
          on_error: "fail",
          cooldown_remaining_s: 87.2,
          last_route: {
            provider: "lmstudio",
            fallback: false,
            status: "error",
            reason: "provider_unavailable",
            error: "LM Link peer disconnected",
            provider_error: "LM Link peer disconnected",
            provider_error_kind: "connection",
            provider_error_hint: "Check LM Link and retry.",
          },
        },
      },
      lmstudio: {
        ok: false,
        detail: "Port 1234 is unavailable",
        model_advertised: null,
        model_ids: [],
      },
    })));

    render(<EmbeddingRuntimePanel />);

    expect(await screen.findByRole("heading", { name: "Active document route" }))
      .toBeInTheDocument();
    expect(screen.getByText(/Dense document and passage features cannot produce vectors/)).toBeInTheDocument();
    expect(screen.getByText("Not loaded")).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText("remote-leaf-ir")).toBeInTheDocument();
    expect(screen.getByText("Cannot inspect while LM Studio is unreachable"))
      .toBeInTheDocument();
    expect(screen.getByText("~526 MB local weight load currently avoided"))
      .toBeInTheDocument();
    expect(screen.getByText(/Remote retry cooldown: 88 seconds/)).toBeInTheDocument();
    expect(screen.getByText(/Remote provider \(connection\): LM Link peer disconnected/))
      .toHaveTextContent("Check LM Link and retry.");
    expect(screen.getByText(/not a guaranteed change in process private commit/))
      .toBeInTheDocument();
  });

  it("renders a recoverable verification error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 503 })));

    render(<EmbeddingRuntimePanel />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Runtime verification is unavailable",
    );
    expect(screen.getByRole("button", { name: "Refresh status" })).toBeEnabled();
  });

  it("does not claim the remote route is reachable when probe evidence is absent", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({
      service_status: "ok",
      policy: {
        effective: "prefer-lmstudio",
        configured: "prefer-lmstudio",
        pending: null,
        apply_status: "effective",
        running: "prefer-lmstudio",
        authority_matches_runtime: true,
      },
      document_model: {
        loaded_locally: false,
        routing: {
          requested_provider: "lmstudio",
          provider_model: "remote-leaf-ir",
          on_error: "fallback",
          cooldown_remaining_s: 0,
          last_route: null,
        },
      },
      lmstudio: null,
    })));

    render(<EmbeddingRuntimePanel />);

    expect(await screen.findByText(/LM Studio reachability was not reported/))
      .toBeInTheDocument();
    expect(screen.getAllByText("Not reported")).toHaveLength(2);
    expect(screen.queryByText(/advertises the configured document model/))
      .not.toBeInTheDocument();
  });

  it("retains the remote failure cause after a successful local fallback", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({
      service_status: "ok",
      policy: {
        effective: "prefer-lmstudio",
        configured: "prefer-lmstudio",
        apply_status: "effective",
        running: "prefer-lmstudio",
        authority_matches_runtime: true,
      },
      document_model: {
        loaded_locally: true,
        routing: {
          requested_provider: "lmstudio",
          provider_model: "remote-leaf-ir",
          on_error: "fallback",
          configuration_status: "ok",
          last_route: {
            provider: "local",
            fallback: true,
            status: "ok",
            reason: "provider_fallback",
            provider_error: "HTTP 503 from LM Studio",
            provider_error_kind: "http",
            provider_error_hint: "Check that the embedding model can load on the peer.",
          },
        },
      },
      lmstudio: { ok: true, model_advertised: false, model_ids: [] },
    })));

    render(<EmbeddingRuntimePanel />);

    expect(await screen.findByText(/Prefer mode may load/)).toBeInTheDocument();
    expect(screen.getByText(/Remote provider \(http\): HTTP 503 from LM Studio/))
      .toHaveTextContent("Check that the embedding model can load on the peer.");
    expect(screen.getByText("Local fallback")).toBeInTheDocument();
  });

  it("surfaces deterministic remote-route misconfiguration before a batch runs", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({
      service_status: "ok",
      policy: {
        effective: "require-lmstudio",
        configured: "require-lmstudio",
        apply_status: "effective",
        running: "require-lmstudio",
        authority_matches_runtime: true,
      },
      document_model: {
        loaded_locally: false,
        routing: {
          requested_provider: "lmstudio",
          provider_model: null,
          on_error: "fail",
          configuration_status: "error",
          configuration_error: "lmstudio_model is required",
          authority_error: null,
          last_route: null,
        },
      },
      lmstudio: { ok: true, model_advertised: null, model_ids: [] },
    })));

    render(<EmbeddingRuntimePanel />);

    expect(await screen.findByText(/remote document route is misconfigured/))
      .toHaveTextContent("lmstudio_model is required");
    expect(screen.getByText(/Require mode blocks document vectors/)).toBeInTheDocument();
    expect(screen.queryByText(/cannot yet verify the complete route/))
      .not.toBeInTheDocument();
  });

  it("surfaces the fail-closed authority safety mode", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({
      service_status: "ok",
      policy: {
        effective: "local",
        configured: "local",
        apply_status: "effective",
        running: "require-lmstudio",
        authority_matches_runtime: false,
      },
      document_model: {
        loaded_locally: false,
        routing: {
          requested_provider: "lmstudio",
          on_error: "fail",
          configuration_status: "error",
          configuration_error: "lmstudio_model is required",
          authority_error: "RuntimeError: settings database unavailable",
          last_route: null,
        },
      },
      lmstudio: null,
    })));

    render(<EmbeddingRuntimePanel />);

    expect(await screen.findByText(/Settings authority could not be read/))
      .toHaveTextContent("fail-closed LM Studio route");
    expect(screen.getByText(/RuntimeError: settings database unavailable/))
      .toBeInTheDocument();
  });

  it("does not mistake a model absent from the advertised list for an unusable route", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({
      service_status: "ok",
      policy: {
        effective: "require-lmstudio",
        configured: "require-lmstudio",
        pending: null,
        apply_status: "effective",
        running: "require-lmstudio",
        authority_matches_runtime: true,
      },
      document_model: {
        loaded_locally: false,
        routing: {
          requested_provider: "lmstudio",
          provider_model: "remote-leaf-ir",
          on_error: "fail",
          cooldown_remaining_s: 0,
          last_route: null,
        },
      },
      lmstudio: {
        ok: true,
        model_advertised: false,
        model_ids: ["text-embedding-nomic-embed-text-v1.5", "other-model"],
      },
    })));

    render(<EmbeddingRuntimePanel />);

    expect(await screen.findByText(/It may still load on demand/))
      .toBeInTheDocument();
    expect(screen.getByText("Not advertised now (may load on demand)")).toBeInTheDocument();
    expect(screen.getByText(/text-embedding-nomic-embed-text-v1\.5, other-model/))
      .toBeInTheDocument();
    expect(screen.queryByText(/advertises the configured document model/))
      .not.toBeInTheDocument();
  });

  it("confirms a remote route from an actual successful document batch", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({
      service_status: "ok",
      policy: {
        effective: "prefer-lmstudio",
        configured: "prefer-lmstudio",
        pending: null,
        apply_status: "effective",
        running: "prefer-lmstudio",
        authority_matches_runtime: true,
      },
      document_model: {
        loaded_locally: false,
        routing: {
          requested_provider: "lmstudio",
          provider_model: "remote-leaf-ir",
          on_error: "fallback",
          cooldown_remaining_s: 0,
          last_route: { provider: "lmstudio", fallback: false, status: "ok" },
        },
      },
      lmstudio: {
        ok: true,
        model_advertised: true,
        model_ids: ["remote-leaf-ir"],
      },
    })));

    render(<EmbeddingRuntimePanel />);

    expect(await screen.findByText(
      "The last document batch completed through LM Studio.",
    )).toBeInTheDocument();
    expect(screen.getByText("Advertised now")).toBeInTheDocument();
  });

  it("does not present a historical route failure as a current outage", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({
      service_status: "ok",
      policy: {
        effective: "require-lmstudio",
        configured: "require-lmstudio",
        apply_status: "effective",
        running: "require-lmstudio",
        authority_matches_runtime: true,
      },
      document_model: {
        loaded_locally: false,
        routing: {
          requested_provider: "lmstudio",
          provider_model: "remote-leaf-ir",
          on_error: "fail",
          configuration_status: "ok",
          cooldown_remaining_s: 0,
          last_route: {
            provider: "lmstudio",
            status: "error",
            reason: "provider_unavailable",
            provider_error: "Previous timeout",
            provider_error_kind: "timeout",
          },
        },
      },
      lmstudio: {
        ok: true,
        model_advertised: true,
        model_ids: ["remote-leaf-ir"],
      },
    })));

    render(<EmbeddingRuntimePanel />);

    expect(await screen.findByText(/last document batch failed through LM Studio/))
      .toHaveTextContent("successful document batch is still needed");
    expect(screen.queryByText(/cannot produce vectors until/)).not.toBeInTheDocument();
    expect(screen.getByText(/Remote provider \(timeout\): Previous timeout/))
      .toBeInTheDocument();
  });

  it("does not mistake missing service evidence for unloaded local weights", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({
      service_status: "unavailable",
      policy: null,
      document_model: null,
      lmstudio: null,
      service_error: "Connection refused",
    })));

    render(<EmbeddingRuntimePanel />);

    expect(await screen.findByText(/The Embedding service is unavailable/))
      .toBeInTheDocument();
    expect(screen.getAllByText("Not reported")).toHaveLength(4);
    expect(screen.getByText("Not reported (~526 MB when leaf-ir loads)"))
      .toBeInTheDocument();
    expect(screen.queryByText("Not loaded")).not.toBeInTheDocument();
  });

  it("uses service health as runtime truth and flags settings-authority drift", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({
      service_status: "ok",
      policy: {
        effective: "require-lmstudio",
        configured: "require-lmstudio",
        pending: null,
        apply_status: "effective",
        running: "local",
        authority_matches_runtime: false,
      },
      document_model: {
        loaded_locally: true,
        routing: {
          requested_provider: "local",
          provider_model: "remote-leaf-ir",
          on_error: "fallback",
          cooldown_remaining_s: 0,
          last_route: { provider: "local", fallback: false, status: "ok" },
        },
      },
      lmstudio: null,
    })));

    render(<EmbeddingRuntimePanel />);

    const mismatch = await screen.findByText((_, element) =>
      element?.getAttribute("role") === "status" &&
      element.textContent?.includes("Settings authority reports Require LM Studio") === true,
    );
    expect(mismatch).toHaveTextContent("Embedding service health reports Local only");
    const activePolicy = screen.getByText("Active policy").closest("div");
    expect(activePolicy).toHaveTextContent("Local only");
    const authority = screen.getByText("Settings effective policy").closest("div");
    expect(authority).toHaveTextContent("Require LM Studio");
    expect(screen.queryByText(/Dense document and passage features cannot produce vectors/))
      .not.toBeInTheDocument();
  });
});
