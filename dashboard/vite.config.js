import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// ATLAS-NOTE: VITE_* vars are read only at dev-server start — restart pnpm dev after .env.local edits.
// VITE_API_URL is the Flask proxy target (Tailscale/LAN IP), not the browser fetch base.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget = env.VITE_API_URL || "http://localhost:5000";

  console.log(`[ATLAS] Proxying /api → ${apiTarget}`);

  return {
    plugins: [react()],
    server: {
      host: true,
      port: 5173,
      strictPort: false,
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: true,
          secure: false,
          configure: (proxy) => {
            proxy.on("error", (err, req) => {
              console.log(`[ATLAS proxy] ${req?.url}: ${err.message} → target ${apiTarget}`);
            });
          },
        },
      },
    },
    build: {
      outDir: "dist",
      emptyOutDir: true,
    },
  };
});
