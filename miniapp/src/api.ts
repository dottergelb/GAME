import { getTelegramInitData } from "./telegram";

const API_BASE = import.meta.env.VITE_API_BASE ?? "https://game-my6i.onrender.com";
const DEV_USER_ID = import.meta.env.VITE_DEV_USER_ID ?? "";
const ALLOW_DEV_FALLBACK = (import.meta.env.VITE_ALLOW_DEV_FALLBACK ?? "true") === "true";

function buildAuthHeaders(): HeadersInit {
  const initData = getTelegramInitData();
  if (initData) {
    return {
      "X-Telegram-Init-Data": initData,
    };
  }

  if (ALLOW_DEV_FALLBACK && DEV_USER_ID) {
    return {
      "X-User-Id": DEV_USER_ID,
    };
  }

  throw new Error(
    "Auth data is missing. Open this app from Telegram or set VITE_DEV_USER_ID for local dev.",
  );
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: buildAuthHeaders(),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text}`);
  }

  return (await res.json()) as T;
}
