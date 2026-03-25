import os
import json
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, unquote_plus

import aiosqlite
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .crud import (
    get_season_rating,
    get_slrpt_rating,
    ensure_user_exists,
    get_me,
)
from .schemas import RatingResponse, MeResponse


# =========================
# APP
# =========================
app = FastAPI(
    title="Telegram MiniApp API",
    version="0.1.0",
)

DB_PATH = os.getenv("SQLITE_PATH", "users.db")
MAX_REPLACEMENTS_PER_MATCH = 2
CORS_ORIGINS = [x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()]
TOURNAMENT_FOUNDER_IDS = {
    int(x.strip())
    for x in os.getenv("TOURNAMENT_FOUNDER_IDS", "5912520356").split(",")
    if x.strip().isdigit()
}


# =========================
# CORS (для Vite miniapp)
# =========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$|^https://.+$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================
# ROOT
# =========================
@app.get("/")
async def root():
    return {"ok": True, "docs": "/docs"}


# =========================
# DEV AUTH HEADER
# =========================
async def get_user_id(
    x_user_id: int | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
):
    """
    В dev-режиме MiniApp шлёт заголовок:

        X-User-Id: 5912520356

    Потом заменим на Telegram initData.
    """
    if x_user_id:
        return x_user_id

    if x_telegram_init_data:
        try:
            parsed = parse_qs(x_telegram_init_data, keep_blank_values=True)
            user_raw = (parsed.get("user") or [None])[0]
            if user_raw:
                user_obj = json.loads(unquote_plus(user_raw))
                uid = int(user_obj.get("id"))
                if uid > 0:
                    return uid
        except Exception:
            pass

    raise HTTPException(status_code=401, detail="Missing auth headers")


class ReplacementCreateBody(BaseModel):
    match_id: int
    out_user_id: int
    in_user_id: int
    reason: str = Field(min_length=3, max_length=500)


class NickCheckCreateBody(BaseModel):
    tournament_id: int
    nickname: str = Field(min_length=3, max_length=24)


class DeputySetBody(BaseModel):
    tournament_id: int
    deputy_user_id: int


class RejectBody(BaseModel):
    reason: str = Field(default="rejected", min_length=1, max_length=500)


class TournamentCreateBody(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    start_date: str = Field(min_length=8, max_length=20)   # DD.MM.YYYY
    end_date: str = Field(min_length=8, max_length=20)     # DD.MM.YYYY
    format_type: str = Field(default="league")             # league | playoff
    max_players: int = Field(default=16, ge=2, le=256)
    match_days: list[int] = Field(default_factory=lambda: [0, 2, 4])
    match_times: list[str] = Field(default_factory=lambda: ["18:00", "19:00"])
    games_per_day: int = Field(default=2, ge=1, le=100)
    prize_pool_rub: int = Field(default=0, ge=0)
    judges: list[int] = Field(default_factory=list)
    semifinal_best_of: int | None = None
    semifinal_slots: list[str] = Field(default_factory=list)
    final_best_of: int | None = None
    final_slots: list[str] = Field(default_factory=list)


async def _fetchone(sql: str, params: tuple = ()) -> tuple | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(sql, params) as cur:
            return await cur.fetchone()


async def _fetchall(sql: str, params: tuple = ()) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(sql, params) as cur:
            return await cur.fetchall()


def _json_load(value: str | None, default: Any):
    if not value:
        return default
    try:
        import json
        return json.loads(value)
    except Exception:
        return default


def _is_tournament_founder(uid: int) -> bool:
    return uid in TOURNAMENT_FOUNDER_IDS


async def _judge_access_for_tournament(uid: int, tournament_id: int) -> bool:
    row = await _fetchone(
        "SELECT creator_id, judges_json, deputy_founder_id, deputy_scope_json FROM tournaments WHERE id=?",
        (tournament_id,),
    )
    if not row:
        return False
    creator_id, judges_json, deputy_founder_id, deputy_scope_json = row
    if uid == int(creator_id):
        return True
    if uid in set(_json_load(judges_json, [])):
        return True
    scopes = set(_json_load(deputy_scope_json, []))
    return uid == deputy_founder_id and ("all" in scopes or "manage_judges" in scopes or "approve_replacements" in scopes)


@app.get("/api/tournaments/capabilities")
async def tournaments_capabilities(uid: int = Depends(get_user_id)):
    tournaments = await _fetchall(
        "SELECT id, creator_id, judges_json, deputy_founder_id, deputy_scope_json FROM tournaments"
    )
    participant_rows = await _fetchall(
        "SELECT DISTINCT tournament_id FROM tournament_players WHERE user_id=?",
        (uid,),
    )
    participant_ids = {int(r[0]) for r in participant_rows}

    can_set_deputy = False
    can_judge_panel = False
    can_manage_requests = False

    for row in tournaments:
        tid, creator_id, judges_json, deputy_id, deputy_scope_json = row
        judges = set(_json_load(judges_json, []))
        scopes = set(_json_load(deputy_scope_json, []))

        is_creator = uid == int(creator_id)
        is_deputy = uid == deputy_id
        is_participant = int(tid) in participant_ids
        is_judge = is_creator or (uid in judges) or (is_deputy and ("all" in scopes or "manage_judges" in scopes or "approve_replacements" in scopes))

        can_set_deputy = can_set_deputy or is_creator
        can_judge_panel = can_judge_panel or is_judge
        can_manage_requests = can_manage_requests or is_participant or is_creator or is_deputy

    return {
        "can_create_tournament": _is_tournament_founder(uid),
        "can_set_deputy": can_set_deputy,
        "can_judge_panel": can_judge_panel,
        "can_manage_requests": can_manage_requests,
    }


# =========================
# /api/me
# =========================
@app.get("/api/me", response_model=MeResponse)
async def api_me(
    uid: int = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """
    Красивый профиль игрока:

    {
      uid,
      nickname,
      season_points,
      matches_played,
      wins,
      slrpt,
      win_mult,
      winrate
    }
    """

    # создаём пользователя если его нет
    await ensure_user_exists(session, uid, nickname=f"user-{uid}")

    row = await get_me(session, uid)
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    # derived winrate
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
    )


# =========================
# /api/rating/season
# =========================
@app.get("/api/rating/season", response_model=RatingResponse)
async def rating_season(
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
):
    """
    Season leaderboard = total_points
    """

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


# =========================
# /api/rating/slrpt
# =========================
@app.get("/api/rating/slrpt", response_model=RatingResponse)
async def rating_slrpt(
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
):
    """
    Offseason leaderboard = slrpt
    """

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


@app.get("/api/tournaments/my-matches")
async def tournaments_my_matches(uid: int = Depends(get_user_id)):
    rows = await _fetchall(
        """
        SELECT id, tournament_id, round_name, scheduled_at, player1_id, player2_id
        FROM tournament_matches
        WHERE status='scheduled' AND (player1_id=? OR player2_id=?)
        ORDER BY scheduled_at ASC, id ASC
        LIMIT 50
        """,
        (uid, uid),
    )
    return {
        "rows": [
            {
                "match_id": r[0],
                "tournament_id": r[1],
                "round_name": r[2],
                "scheduled_at": r[3],
                "player1_id": r[4],
                "player2_id": r[5],
            }
            for r in rows
        ]
    }


@app.get("/api/tournaments/my-open-requests")
async def tournaments_my_open_requests(uid: int = Depends(get_user_id)):
    rep = await _fetchall(
        """
        SELECT id, tournament_id, match_id, out_user_id, in_user_id, reason, created_at
        FROM tournament_replacement_requests
        WHERE created_by=? AND status='open'
        ORDER BY id DESC
        LIMIT 50
        """,
        (uid,),
    )
    nick = await _fetchall(
        """
        SELECT id, tournament_id, user_id, requested_nickname, created_at
        FROM tournament_nickname_checks
        WHERE created_by=? AND status='open'
        ORDER BY id DESC
        LIMIT 50
        """,
        (uid,),
    )
    return {
        "replacement_requests": [
            {
                "id": r[0],
                "tournament_id": r[1],
                "match_id": r[2],
                "out_user_id": r[3],
                "in_user_id": r[4],
                "reason": r[5],
                "created_at": r[6],
            }
            for r in rep
        ],
        "nickname_checks": [
            {
                "id": r[0],
                "tournament_id": r[1],
                "user_id": r[2],
                "requested_nickname": r[3],
                "created_at": r[4],
            }
            for r in nick
        ],
    }


@app.post("/api/tournaments/replacement-requests")
async def tournaments_create_replacement(body: ReplacementCreateBody, uid: int = Depends(get_user_id)):
    m = await _fetchone(
        "SELECT tournament_id, player1_id, player2_id, status, scheduled_at FROM tournament_matches WHERE id=?",
        (body.match_id,),
    )
    if not m:
        raise HTTPException(status_code=404, detail="Match not found")
    tournament_id, p1, p2, status, scheduled_at = m
    if status != "scheduled":
        raise HTTPException(status_code=400, detail="Match is not active")
    if uid not in (p1, p2):
        raise HTTPException(status_code=403, detail="Only match player can create replacement request")
    if body.out_user_id not in (p1, p2):
        raise HTTPException(status_code=400, detail="out_user_id must be current match player")
    if body.in_user_id in (p1, p2) or body.in_user_id == body.out_user_id:
        raise HTTPException(status_code=400, detail="in_user_id must be another player")
    if scheduled_at:
        try:
            match_time = datetime.fromisoformat(scheduled_at)
            if datetime.utcnow() >= match_time:
                raise HTTPException(status_code=400, detail="Replacement is forbidden after match start time")
        except ValueError:
            pass
    already = await _fetchone(
        """
        SELECT COUNT(*) FROM tournament_replacement_requests
        WHERE match_id=? AND status='approved'
        """,
        (body.match_id,),
    )
    if already and int(already[0]) >= MAX_REPLACEMENTS_PER_MATCH:
        raise HTTPException(status_code=400, detail="Replacement limit reached for this match")
    reg = await _fetchone(
        "SELECT 1 FROM tournament_players WHERE tournament_id=? AND user_id=?",
        (tournament_id, body.in_user_id),
    )
    if not reg:
        raise HTTPException(status_code=400, detail="Replacement player is not registered in tournament")
    busy = await _fetchone(
        """
        SELECT 1 FROM tournament_matches
        WHERE tournament_id=? AND status='scheduled' AND id != ?
          AND (player1_id=? OR player2_id=?)
        LIMIT 1
        """,
        (tournament_id, body.match_id, body.in_user_id, body.in_user_id),
    )
    if busy:
        raise HTTPException(status_code=400, detail="Replacement player is already in another active match")

    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO tournament_replacement_requests(
                tournament_id, match_id, out_user_id, in_user_id, reason, status, created_by, created_at
            ) VALUES(?,?,?,?,?,'open',?,?)
            """,
            (tournament_id, body.match_id, body.out_user_id, body.in_user_id, body.reason, uid, now),
        )
        req_id = int(cur.lastrowid or 0)
        await db.execute(
            """
            INSERT INTO tournament_action_log(tournament_id, actor_user_id, action, entity_type, entity_id, details_json, created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                tournament_id,
                uid,
                "replacement_request_create",
                "tournament_replacement_requests",
                req_id,
                json.dumps(
                    {
                        "match_id": body.match_id,
                        "out_user_id": body.out_user_id,
                        "in_user_id": body.in_user_id,
                        "reason": body.reason,
                    },
                    ensure_ascii=False,
                ),
                now,
            ),
        )
        await db.execute(
            """
            INSERT INTO tournament_sync_jobs(tournament_id, job_type, payload_json, status, attempts, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                tournament_id,
                "replacement_request_create",
                f'{{"text":"Replacement request #{req_id} created for match #{body.match_id}"}}',
                "pending",
                0,
                now,
                now,
            ),
        )
        await db.commit()
    return {"ok": True, "request_id": req_id}


@app.post("/api/tournaments/nickname-checks")
async def tournaments_create_nick_check(body: NickCheckCreateBody, uid: int = Depends(get_user_id)):
    t = await _fetchone(
        "SELECT id, creator_id, deputy_founder_id, deputy_scope_json FROM tournaments WHERE id=?",
        (body.tournament_id,),
    )
    if not t:
        raise HTTPException(status_code=404, detail="Tournament not found")
    _, creator_id, deputy_id, deputy_scope_json = t
    reg = await _fetchone(
        "SELECT 1 FROM tournament_players WHERE tournament_id=? AND user_id=?",
        (body.tournament_id, uid),
    )
    scopes = set(_json_load(deputy_scope_json, []))
    is_deputy_manager = uid == deputy_id and ("all" in scopes or "manage_tournament" in scopes)
    if not reg and uid not in (creator_id, deputy_id) and not is_deputy_manager:
        raise HTTPException(status_code=403, detail="Only participant can request nickname check")
    if not body.nickname or len(body.nickname.strip()) < 3:
        raise HTTPException(status_code=400, detail="Nickname is too short")

    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO tournament_nickname_checks(
                tournament_id, user_id, requested_nickname, status, created_by, created_at
            ) VALUES(?,?,?,'open',?,?)
            """,
            (body.tournament_id, uid, body.nickname.strip(), uid, now),
        )
        req_id = int(cur.lastrowid or 0)
        await db.execute(
            """
            INSERT INTO tournament_sync_jobs(tournament_id, job_type, payload_json, status, attempts, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                body.tournament_id,
                "nickname_check_create",
                f'{{"text":"Nickname check request #{req_id} created"}}',
                "pending",
                0,
                now,
                now,
            ),
        )
        await db.commit()
    return {"ok": True, "request_id": req_id}


@app.post("/api/tournaments/deputy")
async def tournaments_set_deputy(body: DeputySetBody, uid: int = Depends(get_user_id)):
    t = await _fetchone(
        "SELECT creator_id FROM tournaments WHERE id=?",
        (body.tournament_id,),
    )
    if not t:
        raise HTTPException(status_code=404, detail="Tournament not found")
    creator_id = int(t[0])
    if uid != creator_id:
        raise HTTPException(status_code=403, detail="Only founder can set deputy")
    scopes = ["manage_participants", "approve_replacements", "manage_judges"]
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tournaments SET deputy_founder_id=?, deputy_scope_json=?, updated_at=? WHERE id=?",
            (body.deputy_user_id, '["manage_participants","approve_replacements","manage_judges"]', now, body.tournament_id),
        )
        await db.execute(
            """
            INSERT INTO tournament_sync_jobs(tournament_id, job_type, payload_json, status, attempts, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                body.tournament_id,
                "deputy_set",
                f'{{"text":"Tournament #{body.tournament_id}: deputy founder set to {body.deputy_user_id}"}}',
                "pending",
                0,
                now,
                now,
            ),
        )
        await db.commit()
    return {"ok": True, "deputy_user_id": body.deputy_user_id, "scopes": scopes}


@app.get("/api/tournaments/sync-status")
async def tournaments_sync_status(uid: int = Depends(get_user_id)):
    _ = uid
    pending = await _fetchone("SELECT COUNT(*) FROM tournament_sync_jobs WHERE status='pending'")
    done = await _fetchone("SELECT COUNT(*) FROM tournament_sync_jobs WHERE status='done'")
    return {
        "pending_jobs": int(pending[0] if pending else 0),
        "done_jobs": int(done[0] if done else 0),
    }


@app.post("/api/tournaments/create")
async def tournaments_create(body: TournamentCreateBody, uid: int = Depends(get_user_id)):
    if not _is_tournament_founder(uid):
        raise HTTPException(status_code=403, detail="Only founder can create tournament")
    if body.format_type not in {"league", "playoff"}:
        raise HTTPException(status_code=400, detail="format_type must be league or playoff")
    for d in body.match_days:
        if d < 0 or d > 6:
            raise HTTPException(status_code=400, detail="match_days values must be from 0 to 6")
    if not body.match_times:
        raise HTTPException(status_code=400, detail="match_times cannot be empty")

    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO tournaments(
                title, creator_id, start_date, end_date,
                match_days_json, match_times_json, games_per_day, max_players, format_type,
                semifinal_best_of, semifinal_slots_json,
                final_best_of, final_slots_json,
                prize_pool_rub, judges_json, status, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                body.title.strip(),
                uid,
                body.start_date.strip(),
                body.end_date.strip(),
                json.dumps(body.match_days, ensure_ascii=False),
                json.dumps(body.match_times, ensure_ascii=False),
                body.games_per_day,
                body.max_players,
                body.format_type,
                body.semifinal_best_of,
                json.dumps(body.semifinal_slots, ensure_ascii=False),
                body.final_best_of,
                json.dumps(body.final_slots, ensure_ascii=False),
                body.prize_pool_rub,
                json.dumps(body.judges, ensure_ascii=False),
                "pending",
                now,
                now,
            ),
        )
        tournament_id = int(cur.lastrowid or 0)
        await db.execute(
            """
            INSERT INTO tournament_action_log(tournament_id, actor_user_id, action, entity_type, entity_id, details_json, created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                tournament_id,
                uid,
                "tournament_create",
                "tournaments",
                tournament_id,
                json.dumps(
                    {
                        "title": body.title,
                        "format_type": body.format_type,
                        "max_players": body.max_players,
                    },
                    ensure_ascii=False,
                ),
                now,
            ),
        )
        await db.commit()
    return {"ok": True, "tournament_id": tournament_id, "status": "pending"}


@app.get("/api/tournaments/judge/open-requests")
async def tournaments_judge_open_requests(uid: int = Depends(get_user_id)):
    rep_rows = await _fetchall(
        """
        SELECT id, tournament_id, match_id, out_user_id, in_user_id, reason, created_by, created_at
        FROM tournament_replacement_requests
        WHERE status='open'
        ORDER BY id DESC
        LIMIT 200
        """
    )
    nick_rows = await _fetchall(
        """
        SELECT id, tournament_id, user_id, requested_nickname, created_by, created_at
        FROM tournament_nickname_checks
        WHERE status='open'
        ORDER BY id DESC
        LIMIT 200
        """
    )
    out_rep = []
    out_nick = []
    for r in rep_rows:
        if await _judge_access_for_tournament(uid, int(r[1])):
            out_rep.append(
                {
                    "id": r[0],
                    "tournament_id": r[1],
                    "match_id": r[2],
                    "out_user_id": r[3],
                    "in_user_id": r[4],
                    "reason": r[5],
                    "created_by": r[6],
                    "created_at": r[7],
                }
            )
    for r in nick_rows:
        if await _judge_access_for_tournament(uid, int(r[1])):
            out_nick.append(
                {
                    "id": r[0],
                    "tournament_id": r[1],
                    "user_id": r[2],
                    "requested_nickname": r[3],
                    "created_by": r[4],
                    "created_at": r[5],
                }
            )
    return {"replacement_requests": out_rep, "nickname_checks": out_nick}


@app.post("/api/tournaments/judge/replacement/{request_id}/approve")
async def judge_approve_replacement(request_id: int, uid: int = Depends(get_user_id)):
    row = await _fetchone(
        """
        SELECT tournament_id, match_id, out_user_id, in_user_id, reason, status, created_by
        FROM tournament_replacement_requests
        WHERE id=?
        """,
        (request_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Replacement request not found")
    tournament_id, match_id, out_uid, in_uid, reason, status, created_by = row
    if status != "open":
        raise HTTPException(status_code=400, detail="Request already decided")
    if not await _judge_access_for_tournament(uid, int(tournament_id)):
        raise HTTPException(status_code=403, detail="Judge access required")
    m = await _fetchone(
        "SELECT player1_id, player2_id, status, scheduled_at FROM tournament_matches WHERE id=?",
        (match_id,),
    )
    if not m:
        raise HTTPException(status_code=404, detail="Match not found")
    p1, p2, m_status, scheduled_at = m
    if m_status != "scheduled":
        raise HTTPException(status_code=400, detail="Match is not active")
    if scheduled_at:
        try:
            if datetime.utcnow() >= datetime.fromisoformat(str(scheduled_at)):
                raise HTTPException(status_code=400, detail="Replacement is forbidden after match start time")
        except ValueError:
            pass
    if out_uid != p1 and out_uid != p2:
        raise HTTPException(status_code=400, detail="out_user_id is no longer in this match")
    already = await _fetchone(
        "SELECT COUNT(*) FROM tournament_replacement_requests WHERE match_id=? AND status='approved'",
        (match_id,),
    )
    if already and int(already[0]) >= MAX_REPLACEMENTS_PER_MATCH:
        raise HTTPException(status_code=400, detail="Replacement limit reached for this match")
    async with aiosqlite.connect(DB_PATH) as db:
        if out_uid == p1:
            await db.execute(
                "UPDATE tournament_matches SET player1_id=?, updated_at=? WHERE id=?",
                (in_uid, datetime.utcnow().isoformat(), match_id),
            )
        else:
            await db.execute(
                "UPDATE tournament_matches SET player2_id=?, updated_at=? WHERE id=?",
                (in_uid, datetime.utcnow().isoformat(), match_id),
            )
        now = datetime.utcnow().isoformat()
        await db.execute(
            "UPDATE tournament_replacement_requests SET status='approved', judge_id=?, decided_at=? WHERE id=?",
            (uid, now, request_id),
        )
        await db.execute(
            """
            INSERT INTO tournament_action_log(tournament_id, actor_user_id, action, entity_type, entity_id, details_json, created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                tournament_id,
                uid,
                "replacement_request_approve",
                "tournament_replacement_requests",
                request_id,
                json.dumps(
                    {"match_id": match_id, "out_user_id": out_uid, "in_user_id": in_uid, "reason": reason},
                    ensure_ascii=False,
                ),
                now,
            ),
        )
        await db.execute(
            """
            INSERT INTO tournament_sync_jobs(tournament_id, job_type, payload_json, status, attempts, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                tournament_id,
                "replacement_approved",
                json.dumps({"text": f"Judge approved replacement in match #{match_id}: {out_uid} -> {in_uid}"}, ensure_ascii=False),
                "pending",
                0,
                now,
                now,
            ),
        )
        await db.commit()
    return {"ok": True, "request_id": request_id}


@app.post("/api/tournaments/judge/replacement/{request_id}/reject")
async def judge_reject_replacement(request_id: int, body: RejectBody, uid: int = Depends(get_user_id)):
    row = await _fetchone(
        "SELECT tournament_id, status, created_by FROM tournament_replacement_requests WHERE id=?",
        (request_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Replacement request not found")
    tournament_id, status, created_by = row
    if status != "open":
        raise HTTPException(status_code=400, detail="Request already decided")
    if not await _judge_access_for_tournament(uid, int(tournament_id)):
        raise HTTPException(status_code=403, detail="Judge access required")
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tournament_replacement_requests SET status='rejected', judge_id=?, decided_at=?, reject_reason=? WHERE id=?",
            (uid, now, body.reason, request_id),
        )
        await db.execute(
            """
            INSERT INTO tournament_action_log(tournament_id, actor_user_id, action, entity_type, entity_id, details_json, created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                tournament_id,
                uid,
                "replacement_request_reject",
                "tournament_replacement_requests",
                request_id,
                json.dumps({"reason": body.reason, "created_by": created_by}, ensure_ascii=False),
                now,
            ),
        )
        await db.commit()
    return {"ok": True, "request_id": request_id}


@app.post("/api/tournaments/judge/nickname/{request_id}/approve")
async def judge_approve_nickname(request_id: int, uid: int = Depends(get_user_id)):
    row = await _fetchone(
        "SELECT tournament_id, user_id, requested_nickname, status FROM tournament_nickname_checks WHERE id=?",
        (request_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Nickname request not found")
    tournament_id, target_user_id, nickname, status = row
    if status != "open":
        raise HTTPException(status_code=400, detail="Request already decided")
    if not await _judge_access_for_tournament(uid, int(tournament_id)):
        raise HTTPException(status_code=403, detail="Judge access required")
    exists = await _fetchone("SELECT user_id FROM users WHERE name=?", (nickname,))
    if exists and int(exists[0]) != int(target_user_id):
        raise HTTPException(status_code=400, detail="Nickname already taken")
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, name)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET name = excluded.name
            """,
            (target_user_id, nickname),
        )
        await db.execute(
            "UPDATE tournament_nickname_checks SET status='approved', judge_id=?, decided_at=? WHERE id=?",
            (uid, now, request_id),
        )
        await db.execute(
            """
            INSERT INTO tournament_action_log(tournament_id, actor_user_id, action, entity_type, entity_id, details_json, created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                tournament_id,
                uid,
                "nickname_check_approve",
                "tournament_nickname_checks",
                request_id,
                json.dumps({"user_id": target_user_id, "nickname": nickname}, ensure_ascii=False),
                now,
            ),
        )
        await db.execute(
            """
            INSERT INTO tournament_sync_jobs(tournament_id, job_type, payload_json, status, attempts, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                tournament_id,
                "nickname_approved",
                json.dumps({"text": f"Judge approved nickname for user {target_user_id}: {nickname}"}, ensure_ascii=False),
                "pending",
                0,
                now,
                now,
            ),
        )
        await db.commit()
    return {"ok": True, "request_id": request_id}


@app.post("/api/tournaments/judge/nickname/{request_id}/reject")
async def judge_reject_nickname(request_id: int, body: RejectBody, uid: int = Depends(get_user_id)):
    row = await _fetchone(
        "SELECT tournament_id, status FROM tournament_nickname_checks WHERE id=?",
        (request_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Nickname request not found")
    tournament_id, status = row
    if status != "open":
        raise HTTPException(status_code=400, detail="Request already decided")
    if not await _judge_access_for_tournament(uid, int(tournament_id)):
        raise HTTPException(status_code=403, detail="Judge access required")
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tournament_nickname_checks SET status='rejected', judge_id=?, decided_at=?, reject_reason=? WHERE id=?",
            (uid, now, body.reason, request_id),
        )
        await db.execute(
            """
            INSERT INTO tournament_action_log(tournament_id, actor_user_id, action, entity_type, entity_id, details_json, created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                tournament_id,
                uid,
                "nickname_check_reject",
                "tournament_nickname_checks",
                request_id,
                json.dumps({"reason": body.reason}, ensure_ascii=False),
                now,
            ),
        )
        await db.commit()
    return {"ok": True, "request_id": request_id}
