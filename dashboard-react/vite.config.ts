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
  server: {
    // Honor an externally assigned port (preview harnesses set PORT);
    // fall back to Vite's default otherwise.
    port: Number(process.env.PORT) || 5173,
    proxy: {
      "/api": "http://127.0.0.1:5127",
      "/favicon.svg": "http://127.0.0.1:5127",
    },
  },
});
