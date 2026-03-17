import aiosqlite
from datetime import datetime
from typing import Optional, List, Tuple

DB_PATH = "users.db"


# =========================
# INIT DB
# =========================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")

        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE,
                name    TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS player_stats (
                user_id        INTEGER PRIMARY KEY,
                total_points   INTEGER DEFAULT 0,
                matches_played INTEGER DEFAULT 0,
                wins           INTEGER DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS verification_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                tg_username TEXT,
                status TEXT NOT NULL DEFAULT 'open',

                game_name TEXT NOT NULL,
                game_uid  TEXT NOT NULL,
                code_word TEXT NOT NULL,

                profile_file_id TEXT NOT NULL,
                chat_file_id TEXT NOT NULL,

                created_at TEXT NOT NULL,
                decided_at TEXT,
                operator_id INTEGER,
                reject_reason TEXT,

                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS verified_accounts (
                user_id INTEGER PRIMARY KEY,
                game_name TEXT NOT NULL,
                game_uid  TEXT NOT NULL UNIQUE,
                verified_at TEXT NOT NULL,
                operator_id INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS name_change_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                old_name TEXT,
                new_name TEXT NOT NULL,
                screenshot_file_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                decided_at TEXT,
                operator_id INTEGER,
                reject_reason TEXT,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            -- Off-season рейтинг и множитель побед
            CREATE TABLE IF NOT EXISTS offseason_rating (
                user_id INTEGER PRIMARY KEY,
                slrpt INTEGER NOT NULL DEFAULT 0,
                win_mult REAL NOT NULL DEFAULT 1.0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                language TEXT NOT NULL DEFAULT 'ru'
            );
            """
        )

        # Ensure user_settings has no FK dependency on users (language can be selected before verification).
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings_new (
                user_id INTEGER PRIMARY KEY,
                language TEXT NOT NULL DEFAULT 'ru'
            )
            """
        )
        try:
            await db.execute(
                """
                INSERT OR REPLACE INTO user_settings_new (user_id, language)
                SELECT user_id, language FROM user_settings
                """
            )
        except Exception:
            pass
        await db.execute("DROP TABLE IF EXISTS user_settings")
        await db.execute("ALTER TABLE user_settings_new RENAME TO user_settings")

        await db.commit()


# =========================
# USERS
# =========================
async def save_user_name(user_id: int, name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")

        async with db.execute(
            "SELECT user_id FROM users WHERE name = ? AND user_id != ?",
            (name, user_id),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return False

        await db.execute(
            """
            INSERT INTO users (user_id, name)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET name = excluded.name
            """,
            (user_id, name),
        )
        await db.commit()
        return True


async def get_user_name(user_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def get_all_user_names() -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name FROM users") as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]


async def get_user_id_by_name(name: str) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE name = ?", (name,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_user_language(user_id: int, language: str) -> None:
    if language not in ("ru", "en"):
        language = "ru"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute(
            """
            INSERT INTO user_settings (user_id, language)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET language = excluded.language
            """,
            (user_id, language),
        )
        await db.commit()


async def get_user_language(user_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT language FROM user_settings WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return row[0] if row[0] in ("ru", "en") else "ru"


# =========================
# PLAYER STATS
# =========================
async def add_points(user_id: int, points: int, is_win: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")

        await db.execute(
            """
            INSERT INTO player_stats (user_id, total_points, matches_played, wins)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                total_points   = total_points   + excluded.total_points,
                matches_played = matches_played + 1,
                wins           = wins           + excluded.wins
            """,
            (user_id, points, 1 if is_win else 0),
        )
        await db.commit()


# =========================
# VERIFICATION REQUESTS
# =========================
async def create_verification_request(
    user_id: int,
    tg_username: Optional[str],
    game_name: str,
    game_uid: str,
    code_word: str,
    profile_file_id: str,
    chat_file_id: str,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute(
            """
            INSERT INTO verification_requests
                (user_id, tg_username, status, game_name, game_uid, code_word,
                 profile_file_id, chat_file_id, created_at)
            VALUES
                (?, ?, 'open', ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                tg_username,
                game_name,
                game_uid,
                code_word,
                profile_file_id,
                chat_file_id,
                datetime.utcnow().isoformat(),
            ),
        )
        await db.commit()
        return cur.lastrowid


async def get_verification_request(req_id: int) -> Optional[Tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT
                id, user_id, tg_username, status,
                game_name, game_uid, code_word,
                profile_file_id, chat_file_id,
                created_at, decided_at, operator_id, reject_reason
            FROM verification_requests
            WHERE id = ?
            """,
            (req_id,),
        ) as cur:
            return await cur.fetchone()


async def set_verification_request_status(
    req_id: int,
    status: str,
    operator_id: int,
    reject_reason: Optional[str] = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute(
            """
            UPDATE verification_requests
            SET status = ?,
                operator_id = ?,
                decided_at = ?,
                reject_reason = ?
            WHERE id = ?
            """,
            (status, operator_id, datetime.utcnow().isoformat(), reject_reason, req_id),
        )
        await db.commit()


async def list_open_verification_requests(limit: int = 20) -> List[Tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, user_id, tg_username, game_name, game_uid, created_at
            FROM verification_requests
            WHERE status = 'open'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            return await cur.fetchall()


# =========================
# VERIFIED ACCOUNTS
# =========================
async def upsert_verified_account(
    user_id: int,
    game_name: str,
    game_uid: str,
    operator_id: int,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute(
            """
            INSERT INTO verified_accounts (user_id, game_name, game_uid, verified_at, operator_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                game_name   = excluded.game_name,
                game_uid    = excluded.game_uid,
                verified_at = excluded.verified_at,
                operator_id = excluded.operator_id
            """,
            (user_id, game_name, game_uid, datetime.utcnow().isoformat(), operator_id),
        )
        await db.commit()


async def is_user_verified(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM verified_accounts WHERE user_id = ? LIMIT 1",
            (user_id,),
        ) as cur:
            if await cur.fetchone():
                return True

        async with db.execute(
            "SELECT 1 FROM verification_requests WHERE user_id = ? AND status = 'approved' LIMIT 1",
            (user_id,),
        ) as cur:
            return (await cur.fetchone()) is not None


# =========================
# NAME CHANGE REQUESTS
# =========================
async def has_open_name_change_request(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM name_change_requests WHERE user_id = ? AND status = 'open' LIMIT 1",
            (user_id,),
        ) as cur:
            return (await cur.fetchone()) is not None


async def create_name_change_request(
    user_id: int,
    old_name: Optional[str],
    new_name: str,
    screenshot_file_id: str,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute(
            """
            INSERT INTO name_change_requests
                (user_id, old_name, new_name, screenshot_file_id, status, created_at)
            VALUES
                (?, ?, ?, ?, 'open', ?)
            """,
            (user_id, old_name, new_name, screenshot_file_id, datetime.utcnow().isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def get_name_change_request(req_id: int) -> Optional[Tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT
                id, user_id, old_name, new_name, screenshot_file_id,
                status, created_at, decided_at, operator_id, reject_reason
            FROM name_change_requests
            WHERE id = ?
            """,
            (req_id,),
        ) as cur:
            return await cur.fetchone()


async def set_name_change_request_status(
    req_id: int,
    status: str,
    operator_id: int,
    reject_reason: Optional[str] = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute(
            """
            UPDATE name_change_requests
            SET status = ?,
                operator_id = ?,
                decided_at = ?,
                reject_reason = ?
            WHERE id = ?
            """,
            (status, operator_id, datetime.utcnow().isoformat(), reject_reason, req_id),
        )
        await db.commit()


async def list_open_name_change_requests(limit: int = 20) -> List[Tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, user_id, old_name, new_name, created_at
            FROM name_change_requests
            WHERE status = 'open'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            return await cur.fetchall()


# =========================
# OFFSEASON SLRPT (ELO-like)
# =========================
def _base_slrpt_delta(current_slrpt: int, place: int) -> int:
    # place: 1..8
    if place < 1:
        place = 1
    if place > 8:
        place = 8

    if current_slrpt < 500:
        table = [40, 30, 20, 10, 0, 0, 0, 0]
    elif current_slrpt < 1500:
        table = [40, 30, 20, 10, -10, -20, -30, -40]
    elif current_slrpt < 2500:
        table = [40, 30, 20, 0, -10, -20, -40, -60]
    else:
        table = [40, 20, 0, -10, -20, -40, -60, -80]

    return table[place - 1]


async def _get_or_create_offseason_row(db: aiosqlite.Connection, user_id: int) -> tuple[int, float]:
    async with db.execute("SELECT slrpt, win_mult FROM offseason_rating WHERE user_id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
        if row:
            return int(row[0]), float(row[1])

    # create default
    await db.execute(
        """
        INSERT INTO offseason_rating (user_id, slrpt, win_mult, updated_at)
        VALUES (?, 0, 1.0, ?)
        """,
        (user_id, datetime.utcnow().isoformat()),
    )
    return 0, 1.0


async def apply_offseason_result(user_id: int, place: int) -> tuple[int, int, float]:
    """
    Returns: (old_slrpt, delta, new_win_mult)

    Rules:
    - Base delta depends on current slrpt tier and place.
    - Win multiplier applies ONLY to T1 reward:
        delta = round(base_delta * win_mult) for place==1
      After T1: win_mult *= 1.1, cap 2.0
    - On T5: win_mult resets to 1.0
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")

        old_slrpt, win_mult = await _get_or_create_offseason_row(db, user_id)
        base = _base_slrpt_delta(old_slrpt, place)

        delta = base
        new_mult = win_mult

        if place == 1:
            delta = int(round(base * win_mult))
            new_mult = min(win_mult * 1.1, 2.0)

        if place == 5:
            new_mult = 1.0

        new_slrpt = old_slrpt + delta
        if new_slrpt < 0:
            new_slrpt = 0
            # если ушли ниже 0, корректируем delta так, чтобы совпало
            delta = -old_slrpt

        await db.execute(
            """
            INSERT INTO offseason_rating (user_id, slrpt, win_mult, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                slrpt = excluded.slrpt,
                win_mult = excluded.win_mult,
                updated_at = excluded.updated_at
            """,
            (user_id, new_slrpt, new_mult, datetime.utcnow().isoformat()),
        )
        await db.commit()

        return old_slrpt, delta, new_mult

