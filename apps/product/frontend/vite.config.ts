import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The backend (:8000) serves the API routes and bundled static matrix.
export default defineConfig(({ mode }) => ({
  plugins: [react(), {
    name: "journey-entry",
    transformIndexHtml: {
      order: "pre",
      handler(html) {
        return mode === "journey"
          ? html.replace("/src/main.tsx", "/src/journey.tsx").replace("AI Search Evaluation Suite", "Trader Journey")
          : html;
      },
    },
  }],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/eval": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
}));
