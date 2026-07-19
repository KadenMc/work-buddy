import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The Flask dashboard serves the built app same-origin at /app, with the
// hashed build output under /app/assets/ (see the react_app routes in
// work_buddy/dashboard/service.py). `base` makes the built index.html
// reference assets at those URLs.
//
// The dev server proxies /api to the local dashboard so the header
// indicators (sidecar via /api/state, live via the /api/events SSE
// stream) work during `npm run dev`. The proxy is a dev convenience
// only: in production the app is same-origin by construction, and the
// browser never fetches a sibling localhost port.
export default defineConfig({
  plugins: [react()],
  base: "/app/",
  // react-draggable's CommonJS development build reads this Node-style flag
  // when a pointer drag starts. Replace only that expression at compile time;
  // a global browser `process` shim would hide other unsafe package assumptions.
  define: {
    "process.env.DRAGGABLE_DEBUG": "false",
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          // The whole React ecosystem lives in ONE chunk. react-aria and
          // react-aria-components import react/react-dom and cross-reference
          // @react-aria / @react-stately, so isolating them in a separate
          // chunk makes Rollup emit two chunks that import each other at module
          // top level. ES module init order then trips a temporal dead zone at
          // runtime ("Cannot access '$' before initialization"). A single chunk
          // is larger but correct, which is the right trade for a same-origin
          // local dashboard. Keep leaf packages (grid, icons) split for caching.
          if (
            id.includes("/react/") ||
            id.includes("/react-dom/") ||
            id.includes("/react-router") ||
            id.includes("react-aria") ||
            id.includes("@react-aria") ||
            id.includes("react-stately") ||
            id.includes("@react-stately") ||
            id.includes("@internationalized")
          ) {
            return "vendor-react";
          }
          if (
            id.includes("react-grid-layout") ||
            id.includes("react-draggable") ||
            id.includes("react-resizable")
          ) {
            return "vendor-grid";
          }
          if (id.includes("@phosphor-icons")) return "vendor-icons";
          return undefined;
        },
      },
    },
  },
  server: {
    // Honor an externally assigned port (preview harnesses set PORT);
    // fall back to Vite's default otherwise.
    port: Number(process.env.PORT) || 5173,
    proxy: {
      "/api": "http://127.0.0.1:5127",
      // base "/app/" makes Vite rewrite the index.html favicon href to
      // /app/favicon.svg in dev. The real logo is served by Flask at the
      // root /favicon.svg (docs/logo.svg, the same asset the legacy
      // dashboard uses), so map both dev URLs onto it. In production the
      // built index.html keeps href="/favicon.svg" and hits that route
      // directly, so this is a dev-only bridge.
      "/favicon.svg": "http://127.0.0.1:5127",
      "/app/favicon.svg": {
        target: "http://127.0.0.1:5127",
        rewrite: () => "/favicon.svg",
      },
    },
  },
});
