import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException

from .settings import settings


def _parse_init_data(init_data: str) -> dict[str, str]:
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    return dict(pairs)


def _verify_init_data(init_data: str) -> dict:
    data = _parse_init_data(init_data)
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="initData hash is missing")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(
        b"WebAppData",
        settings.BOT_TOKEN.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram initData signature")

    auth_date_raw = data.get("auth_date")
    if not auth_date_raw:
        raise HTTPException(status_code=401, detail="initData auth_date is missing")
    try:
        auth_date = int(auth_date_raw)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="initData auth_date is invalid") from exc

    now = int(time.time())
    age = now - auth_date
    if age < -60:
        raise HTTPException(status_code=401, detail="initData auth_date is in the future")
    if age > settings.TELEGRAM_AUTH_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="initData is expired")

    user_raw = data.get("user")
    if not user_raw:
        raise HTTPException(status_code=401, detail="initData user is missing")
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=401, detail="initData user payload is invalid") from exc

    if not isinstance(user, dict) or "id" not in user:
        raise HTTPException(status_code=401, detail="initData user payload is incomplete")

    return user


async def get_tg_user(
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    x_user_id: int | None = Header(default=None, alias="X-User-Id"),
) -> dict:
    if x_telegram_init_data:
        return _verify_init_data(x_telegram_init_data)

    if settings.ALLOW_DEV_AUTH and x_user_id:
        return {
            "id": x_user_id,
            "username": f"user{x_user_id}",
            "first_name": "Dev",
            "last_name": "",
        }

    if settings.ALLOW_DEV_AUTH:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Telegram-Init-Data header (or X-User-Id in dev mode)",
        )

    raise HTTPException(status_code=401, detail="Missing X-Telegram-Init-Data header")
