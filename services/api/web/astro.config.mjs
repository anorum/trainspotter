import { defineConfig } from "astro/config";
import preact from "@astrojs/preact";

// Static output: the FastAPI container serves dist/ as plain files. In dev,
// /api proxies to localhost:8000; run scripts/dev-web.sh to port-forward the
// cluster's api Service there and start this dev server on top of it.
export default defineConfig({
  // The product's own address. Social scrapers only follow absolute URLs, so
  // the share card and canonical link are built from this.
  site: "https://pdxtrain.com",
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
