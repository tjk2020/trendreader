// Minimal service worker — just enough to satisfy PWA installability
// requirements. Deliberately does NOT cache /api/analyze responses,
// since market data must always be fresh.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', () => self.clients.claim());
self.addEventListener('fetch', () => {
  // no-op passthrough — always hit the network
});
