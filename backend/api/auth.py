from fastapi import Header

async def get_tg_user(x_user_id: int | None = Header(default=None)) -> dict:
    """
    Упрощённая версия без проверки Telegram.

    Если передан заголовок:
        X-User-Id: 123

    → вернёт user с этим id.

    Если нет — вернёт дефолтного dev пользователя.
    """

    uid = x_user_id or 1

    return {
        "id": uid,
        "username": f"user{uid}",
        "first_name": "Dev",
        "last_name": "",
    }