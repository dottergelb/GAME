export type TelegramMiniAppUser = {
  id: number;
  username?: string;
  first_name?: string;
  last_name?: string;
};

export function getTelegramWebApp() {
  return (window as any).Telegram?.WebApp;
}

export function initTelegramWebApp(): void {
  const webApp = getTelegramWebApp();
  if (!webApp) return;
  webApp.ready();
  webApp.expand();
}

export function getTelegramInitData(): string | null {
  const raw = getTelegramWebApp()?.initData?.trim();
  if (raw) return raw;

  // Fallback for clients where WebApp object is delayed/unavailable:
  // Telegram may provide init data in URL params.
  const fromQuery = new URLSearchParams(window.location.search).get("tgWebAppData");
  if (fromQuery?.trim()) return decodeURIComponent(fromQuery.trim());

  const hash = window.location.hash.startsWith("#")
    ? window.location.hash.slice(1)
    : window.location.hash;
  const fromHash = new URLSearchParams(hash).get("tgWebAppData");
  if (fromHash?.trim()) return decodeURIComponent(fromHash.trim());

  return null;
}

export function getTelegramUser(): TelegramMiniAppUser | null {
  return getTelegramWebApp()?.initDataUnsafe?.user ?? null;
}

export function sendTelegramData(payload: unknown): boolean {
  const webApp = getTelegramWebApp();
  if (!webApp?.sendData) return false;
  try {
    const data = typeof payload === "string" ? payload : JSON.stringify(payload);
    webApp.sendData(data);
    return true;
  } catch {
    return false;
  }
}
