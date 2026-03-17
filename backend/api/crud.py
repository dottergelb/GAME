from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def get_season_rating(session: AsyncSession, limit: int = 100):
    q = text("""
        SELECT
            u.user_id AS uid,
            u.name    AS nickname,
            COALESCE(ps.total_points, 0) AS points
        FROM users u
        LEFT JOIN player_stats ps ON ps.user_id = u.user_id
        ORDER BY points DESC, u.user_id ASC
        LIMIT :limit
    """)
    res = await session.execute(q, {"limit": limit})
    return res.mappings().all()


async def get_slrpt_rating(session: AsyncSession, limit: int = 100):
    q = text("""
        SELECT
            u.user_id AS uid,
            u.name    AS nickname,
            COALESCE(r.slrpt, 0) AS points
        FROM users u
        LEFT JOIN offseason_rating r ON r.user_id = u.user_id
        ORDER BY points DESC, u.user_id ASC
        LIMIT :limit
    """)
    res = await session.execute(q, {"limit": limit})
    return res.mappings().all()


async def ensure_user_exists(session: AsyncSession, uid: int, nickname: str) -> None:
    q = text("""
        INSERT INTO users (user_id, name)
        VALUES (:uid, :name)
        ON CONFLICT(user_id) DO NOTHING
    """)
    await session.execute(q, {"uid": uid, "name": nickname})


async def get_me(session: AsyncSession, uid: int):
    q = text("""
        SELECT
            u.user_id AS uid,
            u.name    AS nickname,

            COALESCE(ps.total_points, 0)     AS season_points,
            COALESCE(ps.matches_played, 0)   AS matches_played,
            COALESCE(ps.wins, 0)             AS wins,

            COALESCE(r.slrpt, 0)             AS slrpt,
            COALESCE(r.win_mult, 1.0)        AS win_mult,

            CASE WHEN va.user_id IS NULL THEN 0 ELSE 1 END AS verified,
            va.game_uid AS game_uid

        FROM users u
        LEFT JOIN player_stats ps       ON ps.user_id = u.user_id
        LEFT JOIN offseason_rating r    ON r.user_id = u.user_id
        LEFT JOIN verified_accounts va  ON va.user_id = u.user_id

        WHERE u.user_id = :uid
        LIMIT 1
    """)
    res = await session.execute(q, {"uid": uid})
    return res.mappings().first()