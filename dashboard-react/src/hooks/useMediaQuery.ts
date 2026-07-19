import { useEffect, useState } from "react";

/**
 * Track a CSS media query as a boolean, re-rendering when it changes. Guarded for
 * environments without matchMedia (jsdom without a stub), where it reports false.
 * Shared by the app shell and the view host so a single breakpoint definition drives
 * every "is this a narrow, hover-less viewport" decision.
 */
export function useMediaQuery(query: string): boolean {
  const read = () =>
    typeof window.matchMedia === "function" && window.matchMedia(query).matches;
  const [matches, setMatches] = useState(read);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(query);
    // Re-read the live result rather than trusting only the change event: some hosts
    // mount the app at a transient zero width and settle to the real size without ever
    // dispatching a MediaQueryList change, which would otherwise leave the value stale.
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    window.addEventListener("resize", update);
    return () => {
      media.removeEventListener("change", update);
      window.removeEventListener("resize", update);
    };
  }, [query]);
  return matches;
}
