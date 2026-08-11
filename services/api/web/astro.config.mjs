import { defineConfig } from "astro/config";
import preact from "@astrojs/preact";

// Static output: the FastAPI container serves dist/ as plain files. In dev,
// /api proxies to a locally running blockade-api (port-forward the cluster
// Kafka and run `blockade-api run`).
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
