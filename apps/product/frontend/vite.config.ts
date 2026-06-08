import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The backend (:8000) serves the API routes and bundled static matrix.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/eval": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
