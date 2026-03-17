from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
from pathlib import Path

try:
    import asyncpg
except ModuleNotFoundError:
    asyncpg = None
from dotenv import load_dotenv


TABLE_ORDER = [
    "users",
    "player_stats",
    "verification_requests",
    "verified_accounts",
    "name_change_requests",
    "offseason_rating",
    "user_settings",
]


def _normalize_db_url(url: str) -> str:
    return url.replace("postgres://", "postgresql://", 1)


def _fetch_rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    cur = conn.execute(f"SELECT * FROM {table}")
    return cur.fetchall()


async def _migrate_table_users(pg: asyncpg.Connection, rows: list[sqlite3.Row]) -> int:
    count = 0
    for r in rows:
        await pg.execute(
            """
            INSERT INTO users (user_id, name)
            VALUES ($1, $2)
            ON CONFLICT(user_id) DO UPDATE SET name = EXCLUDED.name
            """,
            r["user_id"],
            r["name"],
        )
        count += 1
    return count


async def _migrate_table_player_stats(pg: asyncpg.Connection, rows: list[sqlite3.Row]) -> int:
    count = 0
    for r in rows:
        await pg.execute(
            """
            INSERT INTO player_stats (user_id, total_points, matches_played, wins)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT(user_id) DO UPDATE SET
                total_points = EXCLUDED.total_points,
                matches_played = EXCLUDED.matches_played,
                wins = EXCLUDED.wins
            """,
            r["user_id"],
            r["total_points"],
            r["matches_played"],
            r["wins"],
        )
        count += 1
    return count


async def _migrate_table_verification_requests(pg: asyncpg.Connection, rows: list[sqlite3.Row]) -> int:
    count = 0
    for r in rows:
        await pg.execute(
            """
            INSERT INTO verification_requests
                (id, user_id, tg_username, status, game_name, game_uid, code_word,
                 profile_file_id, chat_file_id, created_at, decided_at, operator_id, reject_reason)
            VALUES
                ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            ON CONFLICT(id) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                tg_username = EXCLUDED.tg_username,
                status = EXCLUDED.status,
                game_name = EXCLUDED.game_name,
                game_uid = EXCLUDED.game_uid,
                code_word = EXCLUDED.code_word,
                profile_file_id = EXCLUDED.profile_file_id,
                chat_file_id = EXCLUDED.chat_file_id,
                created_at = EXCLUDED.created_at,
                decided_at = EXCLUDED.decided_at,
                operator_id = EXCLUDED.operator_id,
                reject_reason = EXCLUDED.reject_reason
            """,
            r["id"],
            r["user_id"],
            r["tg_username"],
            r["status"],
            r["game_name"],
            r["game_uid"],
            r["code_word"],
            r["profile_file_id"],
            r["chat_file_id"],
            r["created_at"],
            r["decided_at"],
            r["operator_id"],
            r["reject_reason"],
        )
        count += 1
    return count


async def _migrate_table_verified_accounts(pg: asyncpg.Connection, rows: list[sqlite3.Row]) -> int:
    count = 0
    for r in rows:
        await pg.execute(
            """
            INSERT INTO verified_accounts (user_id, game_name, game_uid, verified_at, operator_id)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT(user_id) DO UPDATE SET
                game_name = EXCLUDED.game_name,
                game_uid = EXCLUDED.game_uid,
                verified_at = EXCLUDED.verified_at,
                operator_id = EXCLUDED.operator_id
            """,
            r["user_id"],
            r["game_name"],
            r["game_uid"],
            r["verified_at"],
            r["operator_id"],
        )
        count += 1
    return count


async def _migrate_table_name_change_requests(pg: asyncpg.Connection, rows: list[sqlite3.Row]) -> int:
    count = 0
    for r in rows:
        await pg.execute(
            """
            INSERT INTO name_change_requests
                (id, user_id, old_name, new_name, screenshot_file_id, status,
                 created_at, decided_at, operator_id, reject_reason)
            VALUES
                ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT(id) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                old_name = EXCLUDED.old_name,
                new_name = EXCLUDED.new_name,
                screenshot_file_id = EXCLUDED.screenshot_file_id,
                status = EXCLUDED.status,
                created_at = EXCLUDED.created_at,
                decided_at = EXCLUDED.decided_at,
                operator_id = EXCLUDED.operator_id,
                reject_reason = EXCLUDED.reject_reason
            """,
            r["id"],
            r["user_id"],
            r["old_name"],
            r["new_name"],
            r["screenshot_file_id"],
            r["status"],
            r["created_at"],
            r["decided_at"],
            r["operator_id"],
            r["reject_reason"],
        )
        count += 1
    return count


async def _migrate_table_offseason_rating(pg: asyncpg.Connection, rows: list[sqlite3.Row]) -> int:
    count = 0
    for r in rows:
        await pg.execute(
            """
            INSERT INTO offseason_rating (user_id, slrpt, win_mult, updated_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT(user_id) DO UPDATE SET
                slrpt = EXCLUDED.slrpt,
                win_mult = EXCLUDED.win_mult,
                updated_at = EXCLUDED.updated_at
            """,
            r["user_id"],
            r["slrpt"],
            r["win_mult"],
            r["updated_at"],
        )
        count += 1
    return count


async def _migrate_table_user_settings(pg: asyncpg.Connection, rows: list[sqlite3.Row]) -> int:
    count = 0
    for r in rows:
        await pg.execute(
            """
            INSERT INTO user_settings (user_id, language)
            VALUES ($1, $2)
            ON CONFLICT(user_id) DO UPDATE SET language = EXCLUDED.language
            """,
            r["user_id"],
            r["language"],
        )
        count += 1
    return count


async def _sync_sequences(pg: asyncpg.Connection) -> None:
    await pg.execute(
        """
        SELECT setval(
            pg_get_serial_sequence('verification_requests', 'id'),
            COALESCE((SELECT MAX(id) FROM verification_requests), 1),
            true
        )
        """
    )
    await pg.execute(
        """
        SELECT setval(
            pg_get_serial_sequence('name_change_requests', 'id'),
            COALESCE((SELECT MAX(id) FROM name_change_requests), 1),
            true
        )
        """
    )


async def run(sqlite_path: Path, database_url: str) -> None:
    from database import init_db

    if not sqlite_path.exists():
        raise RuntimeError(f"SQLite file not found: {sqlite_path}")

    # Ensure target schema exists in Postgres.
    await init_db()

    sqlite_conn = sqlite3.connect(str(sqlite_path))
    sqlite_conn.row_factory = sqlite3.Row

    pg = await asyncpg.connect(_normalize_db_url(database_url))
    try:
        rows_by_table = {table: _fetch_rows(sqlite_conn, table) for table in TABLE_ORDER}

        migrated = {}
        migrated["users"] = await _migrate_table_users(pg, rows_by_table["users"])
        migrated["player_stats"] = await _migrate_table_player_stats(pg, rows_by_table["player_stats"])
        migrated["verification_requests"] = await _migrate_table_verification_requests(
            pg, rows_by_table["verification_requests"]
        )
        migrated["verified_accounts"] = await _migrate_table_verified_accounts(pg, rows_by_table["verified_accounts"])
        migrated["name_change_requests"] = await _migrate_table_name_change_requests(
            pg, rows_by_table["name_change_requests"]
        )
        migrated["offseason_rating"] = await _migrate_table_offseason_rating(pg, rows_by_table["offseason_rating"])
        migrated["user_settings"] = await _migrate_table_user_settings(pg, rows_by_table["user_settings"])

        await _sync_sequences(pg)

        print("Migration completed.")
        for table in TABLE_ORDER:
            print(f"  {table}: {migrated.get(table, 0)} rows")
    finally:
        sqlite_conn.close()
        await pg.close()


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")

    parser = argparse.ArgumentParser(description="Migrate data from SQLite to Postgres")
    parser.add_argument("--sqlite", default=os.getenv("SQLITE_PATH", "users.db"), help="Path to sqlite database file")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""), help="Postgres connection URL")
    args = parser.parse_args()

    database_url = (args.database_url or "").strip()
    if not database_url:
        print("ERROR: DATABASE_URL is required.")
        return 1
    if asyncpg is None:
        print("ERROR: asyncpg is not installed. Run: pip install -r requirements.txt")
        return 1

    sqlite_path = Path(args.sqlite)
    if not sqlite_path.is_absolute():
        sqlite_path = (Path(__file__).resolve().parents[1] / sqlite_path).resolve()

    try:
        asyncio.run(run(sqlite_path, database_url))
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
