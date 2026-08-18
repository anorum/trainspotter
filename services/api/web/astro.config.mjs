import { defineConfig } from "astro/config";
import preact from "@astrojs/preact";

// Static output: the FastAPI container serves dist/ as plain files. In dev,
// /api proxies to localhost:8000; run scripts/dev-web.sh to port-forward the
// cluster's api Service there and start this dev server on top of it.
export default defineConfig({
  output: "static",
  integrations: [preact()],
  vite: {
    server: {
      proxy: {
        "/api": "http://localhost:8000",
      },
    },
  },
});
