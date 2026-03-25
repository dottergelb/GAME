import { getTelegramInitData } from "./telegram";

function normalizeApiBase(raw: string): string {
  const fallback = "https://game-my6i.onrender.com";
  const value = raw.trim();
  if (!value) return fallback;

  const prefixed = value.startsWith("https://")
    ? value
    : value.startsWith("http://")
      ? `https://${value.slice("http://".length)}`
      : `https://${value}`;

  try {
    const parsed = new URL(prefixed);
    const host = parsed.hostname.toLowerCase();
    const isLocalHost =
      host === "localhost" ||
      host === "127.0.0.1" ||
      host === "0.0.0.0" ||
      host.endsWith(".local") ||
      host.startsWith("10.") ||
      host.startsWith("192.168.") ||
      /^172\.(1[6-9]|2\d|3[0-1])\./.test(host);
    const runningLocal = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1";

    if (isLocalHost && !runningLocal) return fallback;
    return `${parsed.origin}${parsed.pathname}`.replace(/\/+$/, "");
  } catch {
    return fallback;
  }
}

const API_BASE = normalizeApiBase(import.meta.env.VITE_API_BASE ?? "");
const DEV_USER_ID = import.meta.env.VITE_DEV_USER_ID ?? "";
const ALLOW_DEV_FALLBACK = (import.meta.env.VITE_ALLOW_DEV_FALLBACK ?? "false") === "true";

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
    "Отсутствуют данные авторизации. Откройте приложение из Telegram или задайте VITE_DEV_USER_ID для локальной разработки.",
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
