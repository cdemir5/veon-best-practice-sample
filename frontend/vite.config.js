import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// `npm run dev` → http://localhost:5173 ; /api ve /config.js compose'daki nginx'e (8080) proxy'lenir.
const target = process.env.VITE_API_PROXY || 'http://localhost:8080'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': target, '/config.js': target },
  },
  build: { sourcemap: false },
})
