import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Same-origin proxy: the browser only ever talks to :5173, so session cookies stay
// SameSite=Strict and no CORS is needed (ADR-0004). Blob uploads go to Azurite the same way.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": process.env.API_PROXY_TARGET ?? "http://localhost:8000",
      "/devstoreaccount1": process.env.BLOB_PROXY_TARGET ?? "http://localhost:10000",
    },
  },
});
