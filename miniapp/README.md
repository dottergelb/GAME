# Leha Mini App Frontend

## Env
Create `miniapp/.env`:

```env
VITE_API_BASE=http://127.0.0.1:8000
VITE_DEV_USER_ID=5912520356
VITE_ALLOW_DEV_FALLBACK=true
```

## Run
```bash
npm install
npm run dev
```

## Build
```bash
npm run build
```

In Telegram production mode keep `VITE_ALLOW_DEV_FALLBACK=false` and use backend auth with `X-Telegram-Init-Data`.
