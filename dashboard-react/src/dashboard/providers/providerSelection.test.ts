import { describe, expect, it } from "vitest";

import { asAppId } from "../contributions/contracts";
import type { ViewProvider } from "./ViewProvider";
import {
  ProviderSelectionError,
  readRequestedProviderId,
  selectViewProvider,
  selectViewProviderFromSearch,
  type ViewProviderRegistration,
} from "./providerSelection";

const provider = { appId: asAppId("example.provider") } as ViewProvider;
const registrations: readonly ViewProviderRegistration[] = [
  { id: "fixture:july11", label: "July 11 fixture", isDemo: true, provider },
  { id: "live", label: "Live", isDemo: false, provider },
];

describe("provider selection", () => {
  it("selects demo mode only through the explicit registered ID", () => {
    expect(
      selectViewProviderFromSearch(registrations, {
        search: "?provider=fixture%3Ajuly11",
        defaultId: "live",
      }),
    ).toMatchObject({ id: "fixture:july11", isDemo: true, selectedExplicitly: true });
    expect(readRequestedProviderId("?provider=fixture%3Ajuly11")).toBe(
      "fixture:july11",
    );
  });

  it("uses a declared default only when no explicit query selection exists", () => {
    expect(selectViewProvider(registrations, { defaultId: "live" })).toMatchObject({
      id: "live",
      isDemo: false,
      selectedExplicitly: false,
    });
  });

  it("rejects unknown explicit modes rather than silently showing demo data", () => {
    expect(() =>
      selectViewProvider(registrations, {
        requestedId: "fixture:missing",
        defaultId: "live",
      }),
    ).toThrow(ProviderSelectionError);
  });
});

