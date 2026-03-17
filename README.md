# Leha Telegram Mini App

## Implemented
- FastAPI backend with Telegram Mini App auth verification (`initData` signature + expiration).
- React/Vite mini app that sends `X-Telegram-Init-Data` to backend.
- Dev fallback auth with `X-User-Id` (`ALLOW_DEV_AUTH=true`).
- Bot keyboard button `🏆 Leaderboard` opens mini app URL from `LEADERBOARD_URL`.

## 1) Configure env
Copy:

```bash
cp .env.example .env
```

Set values:
- `BOT_TOKEN` - Telegram bot token.
- `LEADERBOARD_URL` - HTTPS URL where your mini app is hosted.
- `CORS_ORIGINS` - allowed frontend origins (comma-separated).
- `OPENAI_API_KEY` - for OCR flow in bot.

For production:
- `ALLOW_DEV_AUTH=false`

## 2) Run backend
```bash
pip install -r requirements.txt
uvicorn backend.api.main:app --host 0.0.0.0 --port 8000 --reload
```

Health check:
- `GET http://127.0.0.1:8000/api/health` -> `{ "ok": true }`

## 3) Run miniapp (dev)
```bash
cd miniapp
npm install
npm run dev
```

`miniapp/.env` example:
```env
VITE_API_BASE=http://127.0.0.1:8000
VITE_DEV_USER_ID=5912520356
VITE_ALLOW_DEV_FALLBACK=true
```

## 4) Deploy miniapp to Telegram
1. Build frontend:
```bash
cd miniapp
npm run build
```
2. Host `miniapp/dist` on HTTPS.
3. Put this URL into:
- root `.env` -> `LEADERBOARD_URL=...`
- BotFather -> bot settings -> Menu Button/Web App URL.
4. Start bot:
```bash
python bot.py
```

## 5) Auth behavior
- If `X-Telegram-Init-Data` is present: backend verifies Telegram signature and user.
- If `ALLOW_DEV_AUTH=true` and Telegram header is missing: backend accepts `X-User-Id`.
- If `ALLOW_DEV_AUTH=false`: only real Telegram auth is accepted.

## 6) SQLite -> Postgres migration
1. Set `DATABASE_URL` in root `.env`.
2. Run:
```bash
python tools/migrate_sqlite_to_postgres.py
```
or on Windows:
```bat
migrate_to_postgres.bat
```
3. Restart backend + bot.

## Windows one-click scripts
- `start_backend.bat` - validate env and run backend
- `start_backend_prod.bat` - run backend with `ALLOW_DEV_AUTH=false`
- `start_bot.bat` - validate env and run bot
- `start_miniapp_dev.bat` - install deps (if needed) and run miniapp dev
- `build_miniapp.bat` - install deps (if needed) and build `miniapp/dist`
- `run_all.bat` - starts all three in separate terminal windows
- `run_prod_local.bat` - starts backend (prod mode) + bot
