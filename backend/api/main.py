from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import get_tg_user
from .crud import ensure_user_exists, get_me, get_season_rating, get_slrpt_rating
from .db import get_session, init_backend_schema
from .schemas import MeResponse, RatingResponse
from .settings import settings


app = FastAPI(
    title="Telegram MiniApp API",
    version="1.0.0",
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MINIAPP_DIST = PROJECT_ROOT / "miniapp" / "dist"

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    await init_backend_schema()


@app.get("/")
async def root():
    return {"ok": True, "docs": "/docs"}


@app.get("/api/health")
async def health():
    return {"ok": True}


if (MINIAPP_DIST / "assets").exists():
    app.mount("/miniapp/assets", StaticFiles(directory=MINIAPP_DIST / "assets"), name="miniapp-assets")


@app.get("/miniapp")
async def miniapp_index():
    if not (MINIAPP_DIST / "index.html").exists():
        raise HTTPException(status_code=404, detail="miniapp build not found")
    return FileResponse(MINIAPP_DIST / "index.html")


@app.get("/miniapp/{path:path}")
async def miniapp_spa(path: str):
    target = MINIAPP_DIST / path
    if target.exists() and target.is_file():
        return FileResponse(target)
    if not (MINIAPP_DIST / "index.html").exists():
        raise HTTPException(status_code=404, detail="miniapp build not found")
    return FileResponse(MINIAPP_DIST / "index.html")


@app.get("/api/me", response_model=MeResponse)
async def api_me(
    tg_user: dict = Depends(get_tg_user),
    session: AsyncSession = Depends(get_session),
):
    uid = int(tg_user["id"])
    nickname = tg_user.get("username") or tg_user.get("first_name") or f"user-{uid}"

    await ensure_user_exists(session, uid, nickname=nickname)

    row = await get_me(session, uid)
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    mp = row["matches_played"]
    wins = row["wins"]
    winrate = wins / mp if mp > 0 else 0.0

    return MeResponse(
        uid=row["uid"],
        nickname=row["nickname"],
        season_points=row["season_points"],
        matches_played=row["matches_played"],
        wins=row["wins"],
        slrpt=row["slrpt"],
        win_mult=row["win_mult"],
        winrate=winrate,
        verified=bool(row["verified"]),
        game_uid=row["game_uid"],
    )


@app.get("/api/rating/season", response_model=RatingResponse)
async def rating_season(
    tg_user: dict = Depends(get_tg_user),
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
):
    _ = tg_user
    rows = await get_season_rating(session, limit)
    return RatingResponse(
        season_id=None,
        rows=[
            {
                "rank": i + 1,
                "uid": r["uid"],
                "nickname": r["nickname"],
                "points": r["points"],
            }
            for i, r in enumerate(rows)
        ],
    )


@app.get("/api/rating/slrpt", response_model=RatingResponse)
async def rating_slrpt(
    tg_user: dict = Depends(get_tg_user),
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
):
    _ = tg_user
    rows = await get_slrpt_rating(session, limit)
    return RatingResponse(
        season_id=None,
        rows=[
            {
                "rank": i + 1,
                "uid": r["uid"],
                "nickname": r["nickname"],
                "points": r["points"],
            }
            for i, r in enumerate(rows)
        ],
    )
