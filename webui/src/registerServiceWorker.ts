export function registerServiceWorker(): void {
  if (!import.meta.env.PROD) return;
  if (!("serviceWorker" in navigator)) return;

  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js?v=3").catch((error: unknown) => {
      console.debug("Ziggy service worker registration failed", error);
    });
  });
}
