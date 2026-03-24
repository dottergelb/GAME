import { getTelegramInitData } from "./telegram";

const API_BASE = import.meta.env.VITE_API_BASE ?? "https://game-my6i.onrender.com";
const DEV_USER_ID = import.meta.env.VITE_DEV_USER_ID ?? "";
const ALLOW_DEV_FALLBACK = (import.meta.env.VITE_ALLOW_DEV_FALLBACK ?? "true") === "true";

function resolveDevUserId(): string {
  if (DEV_USER_ID) return DEV_USER_ID;

  const fromQuery = new URLSearchParams(window.location.search).get("uid") ?? "";
  if (fromQuery) return fromQuery;

  const fromStorage = window.localStorage.getItem("dev_user_id") ?? "";
  if (fromStorage) return fromStorage;

  if (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1") {
    return "5912520356";
  }

  return "";
}

function buildAuthHeaders(): HeadersInit {
  const initData = getTelegramInitData();
  if (initData) {
    return {
      "X-Telegram-Init-Data": initData,
    };
  }

  const devUserId = resolveDevUserId();
  if (ALLOW_DEV_FALLBACK && devUserId) {
    return {
      "X-User-Id": devUserId,
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

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      ...buildAuthHeaders(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text}`);
  }

  return (await res.json()) as T;
}
