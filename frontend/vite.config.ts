import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the API runs on :8000; proxying keeps the SPA and the API on one origin,
// which the SameSite=Strict refresh cookie requires. In production FastAPI serves dist/.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": "http://localhost:8000" } },
  preview: { port: 4173, proxy: { "/api": "http://localhost:8000" } },
});
