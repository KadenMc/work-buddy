import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const dashboardProxyTarget =
  process.env.WB_DASHBOARD_PROXY_TARGET || "http://127.0.0.1:5127";

const dashboardProxy = {
  // Local identity binds the browser Origin to its loopback Host. Preserve
  // that Host through dev/preview rather than rewriting it to the API port.
  "/api": { target: dashboardProxyTarget, changeOrigin: false },
  "/favicon.svg": dashboardProxyTarget,
  "/app/favicon.svg": {
    target: dashboardProxyTarget,
    rewrite: () => "/favicon.svg",
  },
};

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
          // The drag/resize leaf family. react-resizable-panels (the Co-work split) only
          // imports react/react-dom, so it rides here with a single one-directional edge to
          // vendor-react and never forms a chunk cycle. It is named explicitly so trimming a
          // sibling term cannot silently relocate it. Its name also contains "react-resizable",
          // so the term below would catch it regardless. The explicit clause is documentation.
          if (
            id.includes("react-grid-layout") ||
            id.includes("react-draggable") ||
            id.includes("react-resizable-panels") ||
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
    // base "/app/" makes Vite rewrite the index.html favicon href to
    // /app/favicon.svg in dev. The real logo is served by Flask at the
    // root /favicon.svg (docs/logo.svg, the same asset the legacy
    // dashboard uses), so map both dev URLs onto it.
    proxy: dashboardProxy,
  },
  preview: {
    // The isolated Co-work lifecycle harness exercises the production build
    // through Vite preview. Keep its same-origin API contract identical to
    // development, including the explicit backend target override.
    proxy: dashboardProxy,
  },
});
