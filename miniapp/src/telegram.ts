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
  return raw ? raw : null;
}

export function getTelegramUser(): TelegramMiniAppUser | null {
  return getTelegramWebApp()?.initDataUnsafe?.user ?? null;
}
