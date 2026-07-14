import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['pwa-192x192.png', 'pwa-512x512.png'],
      manifest: {
        name: '축구 승무패 예측',
        short_name: '승무패예측',
        description: 'EPL / K리그1 / K리그2 / 분데스리가 승무패 예측',
        theme_color: '#125a3c',
        background_color: '#0b1f17',
        display: 'standalone',
        start_url: '/',
        icons: [
          { src: 'pwa-192x192.png', sizes: '192x192', type: 'image/png' },
          { src: 'pwa-512x512.png', sizes: '512x512', type: 'image/png' },
        ],
      },
      workbox: {
        // Cache the last predictions/fixtures response so the app still
        // shows something (last-known data) when offline.
        runtimeCaching: [
          {
            urlPattern: ({ url }) => url.pathname === '/fixtures' || url.pathname === '/predictions',
            handler: 'NetworkFirst',
            options: { cacheName: 'api-cache', networkTimeoutSeconds: 4 },
          },
        ],
      },
    }),
  ],
})
