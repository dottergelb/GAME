import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Any, Callable, Iterable, Optional

import aiosqlite
from aiogram import F, Bot, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

from database import DB_PATH, get_user_name


DATE_FMT = "%d.%m.%Y"
TIME_FMT = "%H:%M"
WEEKDAY_ALIASES = {
    "mon": 0, "monday": 0, "пн": 0, "пон": 0, "понедельник": 0,
    "tue": 1, "tuesday": 1, "вт": 1, "втор": 1, "вторник": 1,
    "wed": 2, "wednesday": 2, "ср": 2, "сред": 2, "среда": 2,
    "thu": 3, "thursday": 3, "чт": 3, "чет": 3, "четверг": 3,
    "fri": 4, "friday": 4, "пт": 4, "пят": 4, "пятница": 4,
    "sat": 5, "saturday": 5, "сб": 5, "суб": 5, "суббота": 5,
    "sun": 6, "sunday": 6, "вс": 6, "воскр": 6, "воскресенье": 6,
}
WEEKDAY_NAMES_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


class TournamentCreate(StatesGroup):
    title = State()
    start_date = State()
    end_date = State()
    match_days = State()
    match_times = State()
    games_per_day = State()
    max_players = State()
    format_type = State()
    semifinal_best_of = State()
    semifinal_slots = State()
    final_best_of = State()
    final_slots = State()
    need_judges = State()
    judges = State()
    prize_pool = State()
    confirm = State()


class TournamentReject(StatesGroup):
    reason = State()


class JudgeResultState(StatesGroup):
    waiting_photo = State()


@dataclass
class TournamentContext:
    operators: set[int]
    get_keyboard_for_user: Callable[[int], Any]


CTX: Optional[TournamentContext] = None


async def init_tournament_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tournaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                creator_id INTEGER NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                match_days_json TEXT NOT NULL,
                match_times_json TEXT NOT NULL,
                games_per_day INTEGER NOT NULL DEFAULT 999,
                max_players INTEGER NOT NULL,
                format_type TEXT NOT NULL,
                semifinal_best_of INTEGER,
                semifinal_slots_json TEXT,
                final_best_of INTEGER,
                final_slots_json TEXT,
                prize_pool_rub INTEGER NOT NULL DEFAULT 0,
                judges_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pending',
                moderator_id INTEGER,
                reject_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tournament_players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                seed INTEGER,
                status TEXT NOT NULL DEFAULT 'registered',
                joined_at TEXT NOT NULL,
                UNIQUE(tournament_id, user_id),
                FOREIGN KEY(tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tournament_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                round_no INTEGER NOT NULL,
                round_name TEXT NOT NULL,
                bracket_position INTEGER NOT NULL,
                scheduled_at TEXT,
                best_of INTEGER NOT NULL DEFAULT 1,
                judge_id INTEGER,
                player1_id INTEGER,
                player2_id INTEGER,
                source_match1_id INTEGER,
                source_match2_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                winner_id INTEGER,
                screenshot_file_id TEXT,
                score_text TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tournament_standings (
                tournament_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                played INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                points INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(tournament_id, user_id),
                FOREIGN KEY(tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
            );
            """
        )
        try:
            await db.execute("ALTER TABLE tournaments ADD COLUMN games_per_day INTEGER NOT NULL DEFAULT 999")
        except Exception:
            pass
        await db.commit()


def _parse_date(text: str) -> date:
    return datetime.strptime(text.strip(), DATE_FMT).date()


def _parse_times(text: str) -> list[str]:
    parts = [p.strip() for p in text.replace(",", " ").split() if p.strip()]
    out = []
    seen = set()
    for p in parts:
        datetime.strptime(p, TIME_FMT)
        if p not in seen:
            seen.add(p)
            out.append(p)
    if not out:
        raise ValueError("Нет времени")
    return out


def _parse_days(text: str) -> list[int]:
    parts = [p.strip().lower() for p in text.replace(",", " ").split() if p.strip()]
    out = []
    seen = set()
    for p in parts:
        if p not in WEEKDAY_ALIASES:
            raise ValueError(f"Неизвестный день: {p}")
        wd = WEEKDAY_ALIASES[p]
        if wd not in seen:
            seen.add(wd)
            out.append(wd)
    if not out:
        raise ValueError("Нет дней")
    return sorted(out)


def _parse_judge_ids(text: str) -> list[int]:
    raw = [x.strip() for x in text.replace("\n", ",").split(",") if x.strip()]
    if not raw:
        return []
    out = []
    for item in raw:
        if item.startswith("@"):
            raise ValueError("Для судей укажи Telegram user_id числами, через запятую")
        try:
            out.append(int(item))
        except ValueError:
            raise ValueError("Judge IDs должны быть числами через запятую")
    return out


def _parse_slot_pairs(text: str) -> list[str]:
    chunks = [c.strip() for c in text.replace("\n", ",").split(",") if c.strip()]
    out = []
    for c in chunks:
        dt = datetime.strptime(c, f"{DATE_FMT} {TIME_FMT}")
        out.append(dt.isoformat())
    if not out:
        raise ValueError("Нужен хотя бы один слот")
    return out


def _fmt_money(v: int) -> str:
    return f"{v:,} ₽".replace(",", " ")


def _next_power_of_two(n: int) -> int:
    p = 1
    while p < n:
        p *= 2
    return p


def _round_label(round_size: int) -> str:
    if round_size == 2:
        return "Финал"
    if round_size == 4:
        return "Полуфинал"
    if round_size == 8:
        return "Четвертьфинал"
    return f"Раунд {round_size}"


def _json_load(s: Optional[str], default):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def _build_slots(start_d: date, end_d: date, days: list[int], times: list[str], games_per_day: int) -> list[str]:
    cur = start_d
    out: list[str] = []
    while cur <= end_d:
        if cur.weekday() in days:
            for tm in times[:games_per_day]:
                dt = datetime.combine(cur, datetime.strptime(tm, TIME_FMT).time())
                out.append(dt.isoformat())
        cur += timedelta(days=1)
    return out


async def _fetchone(query: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(query, params) as cur:
            return await cur.fetchone()


async def _fetchall(query: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(query, params) as cur:
            return await cur.fetchall()


async def create_tournament(data: dict[str, Any]) -> int:
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
                data["title"],
                data["creator_id"],
                data["start_date"],
                data["end_date"],
                json.dumps(data["match_days"]),
                json.dumps(data["match_times"]),
                data["games_per_day"],
                data["max_players"],
                data["format_type"],
                data.get("semifinal_best_of"),
                json.dumps(data.get("semifinal_slots") or []),
                data.get("final_best_of"),
                json.dumps(data.get("final_slots") or []),
                data["prize_pool_rub"],
                json.dumps(data.get("judges") or []),
                "pending",
                now,
                now,
            ),
        )
        await db.commit()
        return cur.lastrowid


async def get_tournament(tournament_id: int) -> Optional[dict[str, Any]]:
    row = await _fetchone(
        "SELECT id, title, creator_id, start_date, end_date, match_days_json, match_times_json, games_per_day, max_players, format_type, semifinal_best_of, semifinal_slots_json, final_best_of, final_slots_json, prize_pool_rub, judges_json, status, moderator_id, reject_reason FROM tournaments WHERE id=?",
        (tournament_id,),
    )
    if not row:
        return None
    return {
        "id": row[0],
        "title": row[1],
        "creator_id": row[2],
        "start_date": row[3],
        "end_date": row[4],
        "match_days": _json_load(row[5], []),
        "match_times": _json_load(row[6], []),
        "games_per_day": row[7],
        "max_players": row[8],
        "format_type": row[9],
        "semifinal_best_of": row[10],
        "semifinal_slots": _json_load(row[11], []),
        "final_best_of": row[12],
        "final_slots": _json_load(row[13], []),
        "prize_pool_rub": row[14],
        "judges": _json_load(row[15], []),
        "status": row[16],
        "moderator_id": row[17],
        "reject_reason": row[18],
    }


async def list_tournaments(statuses: Optional[Iterable[str]] = None, limit: int = 20) -> list[dict[str, Any]]:
    if statuses:
        statuses = list(statuses)
        placeholders = ",".join(["?"] * len(statuses))
        rows = await _fetchall(
            f"SELECT id, title, format_type, status, start_date, end_date, max_players, prize_pool_rub FROM tournaments WHERE status IN ({placeholders}) ORDER BY id DESC LIMIT ?",
            tuple(statuses) + (limit,),
        )
    else:
        rows = await _fetchall(
            "SELECT id, title, format_type, status, start_date, end_date, max_players, prize_pool_rub FROM tournaments ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    return [
        {
            "id": r[0], "title": r[1], "format_type": r[2], "status": r[3],
            "start_date": r[4], "end_date": r[5], "max_players": r[6], "prize_pool_rub": r[7],
        }
        for r in rows
    ]


async def count_tournament_players(tournament_id: int) -> int:
    row = await _fetchone("SELECT COUNT(*) FROM tournament_players WHERE tournament_id=?", (tournament_id,))
    return int(row[0] if row else 0)


async def is_registered(tournament_id: int, user_id: int) -> bool:
    row = await _fetchone("SELECT 1 FROM tournament_players WHERE tournament_id=? AND user_id=?", (tournament_id, user_id))
    return bool(row)


async def register_player(tournament_id: int, user_id: int) -> bool:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT max_players, status FROM tournaments WHERE id=?",
            (tournament_id,),
        ) as cur:
            trow = await cur.fetchone()

        if not trow or trow[1] != "approved":
            return False

        async with db.execute(
            "SELECT COUNT(*) FROM tournament_players WHERE tournament_id=?",
            (tournament_id,),
        ) as cur:
            cnt = await cur.fetchone()

        if cnt and cnt[0] >= trow[0]:
            return False

        try:
            await db.execute(
                "INSERT INTO tournament_players(tournament_id, user_id, joined_at) VALUES(?,?,?)",
                (tournament_id, user_id, now),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def unregister_player(tournament_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM tournament_players WHERE tournament_id=? AND user_id=?", (tournament_id, user_id))
        await db.commit()
        return cur.rowcount > 0


async def set_tournament_status(tournament_id: int, status: str, moderator_id: Optional[int] = None, reject_reason: Optional[str] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tournaments SET status=?, moderator_id=COALESCE(?, moderator_id), reject_reason=?, updated_at=? WHERE id=?",
            (status, moderator_id, reject_reason, datetime.utcnow().isoformat(), tournament_id),
        )
        await db.commit()


async def get_tournament_players(tournament_id: int) -> list[int]:
    rows = await _fetchall("SELECT user_id FROM tournament_players WHERE tournament_id=? ORDER BY joined_at ASC", (tournament_id,))
    return [r[0] for r in rows]


async def get_open_matches_for_judge(judge_id: int) -> list[tuple]:
    return await _fetchall(
        "SELECT id, tournament_id, round_name, scheduled_at, player1_id, player2_id FROM tournament_matches WHERE judge_id=? AND status='scheduled' ORDER BY scheduled_at ASC",
        (judge_id,),
    )


async def get_match(match_id: int):
    return await _fetchone(
        "SELECT id, tournament_id, round_no, round_name, bracket_position, scheduled_at, best_of, judge_id, player1_id, player2_id, source_match1_id, source_match2_id, status, winner_id, screenshot_file_id, score_text FROM tournament_matches WHERE id=?",
        (match_id,),
    )


def _tournament_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🏁 Playoff", callback_data="tourfmt|playoff"),
        InlineKeyboardButton(text="📊 Таблица", callback_data="tourfmt|league"),
    ]])


def _yes_no_kb(yes_cb: str, no_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да", callback_data=yes_cb),
        InlineKeyboardButton(text="Нет", callback_data=no_cb),
    ]])


def _summary_text(data: dict[str, Any]) -> str:
    lines = [
        "🏆 Заявка на турнир",
        f"Название: {data['title']}",
        f"Формат: {'Playoff' if data['format_type']=='playoff' else 'Турнирная таблица'}",
        f"Старт: {data['start_date']}",
        f"Конец: {data['end_date']}",
        f"Игровые дни: {' '.join(WEEKDAY_NAMES_RU[d] for d in data['match_days'])}",
        f"Время матчей: {', '.join(data['match_times'])}",
        f"Матчей в день: {data['games_per_day']}",
        f"Макс. игроков: {data['max_players']}",
        f"Судьи: {', '.join(map(str, data.get('judges', []))) if data.get('judges') else 'не назначены'}",
        f"Призовой фонд: {_fmt_money(data['prize_pool_rub'])}",
    ]
    if data['format_type'] == 'playoff':
        lines += [
            f"Полуфинал: BO{data['semifinal_best_of']} | слоты: {', '.join(data['semifinal_slots'])}",
            f"Финал: BO{data['final_best_of']} | слоты: {', '.join(data['final_slots'])}",
        ]
    return "\n".join(lines)


async def _tournament_text(t: dict[str, Any]) -> str:
    cnt = await count_tournament_players(t['id'])
    lines = [
        f"🏆 {t['title']} (#{t['id']})",
        f"Статус: {t['status']}",
        f"Формат: {'Playoff' if t['format_type']=='playoff' else 'Турнирная таблица'}",
        f"Даты: {t['start_date']} — {t['end_date']}",
        f"Игровые дни: {' '.join(WEEKDAY_NAMES_RU[d] for d in t['match_days'])}",
        f"Время: {', '.join(t['match_times'])}",
        f"Матчей в день: {t['games_per_day']}",
        f"Игроки: {cnt}/{t['max_players']}",
        f"Призовой фонд: {_fmt_money(t['prize_pool_rub'])}",
    ]
    if t['judges']:
        lines.append(f"Судьи: {', '.join(map(str, t['judges']))}")
    if t['format_type'] == 'playoff':
        lines.append(f"Полуфинал: BO{t['semifinal_best_of']}, финал: BO{t['final_best_of']}")
    if t.get('reject_reason'):
        lines.append(f"Причина отклонения: {t['reject_reason']}")
    return "\n".join(lines)


def _tournament_card_kb(t: dict[str, Any], viewer_id: int) -> InlineKeyboardMarkup:
    rows = []
    if t['status'] == 'approved':
        rows.append([InlineKeyboardButton(text='📝 Зарегистрироваться', callback_data=f'tourreg|{t["id"]}')])
    if t['status'] in ('approved', 'active', 'finished'):
        rows.append([InlineKeyboardButton(text='📋 Матчи/сетка', callback_data=f'tourmatches|{t["id"]}')])
    if viewer_id == t['creator_id'] or (CTX and viewer_id in CTX.operators):
        rows.append([InlineKeyboardButton(text='⚙️ Управление', callback_data=f'tourmanage|{t["id"]}')])
    return InlineKeyboardMarkup(inline_keyboard=rows or [[InlineKeyboardButton(text='Закрыть', callback_data='noop')]])


def _moderation_kb(tournament_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='✅ Принять', callback_data=f'tourapprove|{tournament_id}')],
        [InlineKeyboardButton(text='❌ Отклонить', callback_data=f'tourreject|{tournament_id}')],
    ])


def _manage_kb(tournament_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🔒 Закрыть регистрацию и сгенерировать', callback_data=f'tourstart|{tournament_id}')],
        [InlineKeyboardButton(text='👥 Участники', callback_data=f'tourplayers|{tournament_id}')],
        [InlineKeyboardButton(text='📋 Матчи/сетка', callback_data=f'tourmatches|{tournament_id}')],
    ])


def _winner_pick_kb(match_id: int, p1_name: str, p2_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f'🏆 {p1_name}', callback_data=f'judgewin|{match_id}|1')],
        [InlineKeyboardButton(text=f'🏆 {p2_name}', callback_data=f'judgewin|{match_id}|2')],
    ])


async def _player_label(user_id: Optional[int]) -> str:
    if not user_id:
        return 'TBD'
    return await get_user_name(user_id) or str(user_id)


async def _assign_judges_and_notify(bot: Bot, tournament_id: int):
    t = await get_tournament(tournament_id)
    if not t:
        return
    judges = list(t['judges'] or [])
    if not judges:
        return
    rows = await _fetchall(
        "SELECT id, round_name, scheduled_at, player1_id, player2_id, judge_id FROM tournament_matches WHERE tournament_id=? AND status='scheduled' ORDER BY scheduled_at ASC, id ASC",
        (tournament_id,),
    )
    idx = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for row in rows:
            mid, round_name, scheduled_at, p1, p2, judge_id = row
            if judge_id:
                continue
            jid = judges[idx % len(judges)]
            idx += 1
            await db.execute("UPDATE tournament_matches SET judge_id=?, updated_at=? WHERE id=?", (jid, datetime.utcnow().isoformat(), mid))
        await db.commit()

    rows = await _fetchall(
        "SELECT id, round_name, scheduled_at, player1_id, player2_id, judge_id FROM tournament_matches WHERE tournament_id=? AND status='scheduled' ORDER BY scheduled_at ASC, id ASC",
        (tournament_id,),
    )
    for mid, round_name, scheduled_at, p1, p2, jid in rows:
        if not jid:
            continue
        p1n = await _player_label(p1)
        p2n = await _player_label(p2)
        dt_text = scheduled_at.replace('T', ' ') if scheduled_at else 'без времени'
        try:
            await bot.send_message(
                jid,
                f"⚖️ Назначен матч турнира #{tournament_id}\n{round_name}\n{p1n} vs {p2n}\nКогда: {dt_text}\n\nНажми кнопку ниже, когда матч закончится.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='📸 Принять скрин результата', callback_data=f'judgeopen|{mid}')]])
            )
        except Exception:
            pass


async def _create_playoff_matches(bot: Bot, tournament_id: int, player_ids: list[int], t: dict[str, Any]):
    random.shuffle(player_ids)
    n = len(player_ids)
    bracket_size = _next_power_of_two(max(2, n))
    rounds = int(math.log2(bracket_size))
    seeded: list[Optional[int]] = player_ids + [None] * (bracket_size - n)

    start_d = _parse_date(t['start_date'])
    end_d = _parse_date(t['end_date'])
    generic_slots = _build_slots(
        start_d,
        end_d,
        t['match_days'],
        t['match_times'],
        t.get('games_per_day', 999),
    )
    generic_iter = iter(generic_slots)
    semi_slots = list(t['semifinal_slots'] or [])
    final_slots = list(t['final_slots'] or [])

    now = datetime.utcnow().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        current_inputs: list[Any] = seeded[:]

        for round_no in range(1, rounds + 1):
            round_size = bracket_size // (2 ** (round_no - 1))
            round_name = _round_label(round_size)
            next_inputs: list[Any] = []

            for pos in range(0, len(current_inputs), 2):
                left = current_inputs[pos]
                right = current_inputs[pos + 1]
                best_of = 1
                if round_name == 'Полуфинал':
                    best_of = t.get('semifinal_best_of') or 1
                    slot = semi_slots[pos // 2] if pos // 2 < len(semi_slots) else next(generic_iter, None)
                elif round_name == 'Финал':
                    best_of = t.get('final_best_of') or 1
                    slot = final_slots[0] if final_slots else next(generic_iter, None)
                else:
                    slot = next(generic_iter, None)

                if round_no == 1:
                    p1 = left if isinstance(left, int) else None
                    p2 = right if isinstance(right, int) else None
                    src1 = None
                    src2 = None
                else:
                    p1 = None
                    p2 = None
                    src1 = left
                    src2 = right

                status = 'pending'
                winner_id = None
                if round_no == 1 and (p1 or p2) and not (p1 and p2):
                    winner_id = p1 or p2
                    status = 'completed'
                    slot = None

                cur = await db.execute(
                    """
                    INSERT INTO tournament_matches(
                        tournament_id, round_no, round_name, bracket_position, scheduled_at, best_of,
                        judge_id, player1_id, player2_id, source_match1_id, source_match2_id,
                        status, winner_id, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        tournament_id, round_no, round_name, pos // 2, slot, best_of,
                        None, p1, p2, src1, src2,
                        status, winner_id, now, now,
                    ),
                )
                mid = cur.lastrowid
                next_inputs.append(mid)

            current_inputs = next_inputs

        await db.commit()

    await _refresh_playoff_ready_matches(tournament_id)
    await _assign_judges_and_notify(bot, tournament_id)


async def _refresh_playoff_ready_matches(tournament_id: int):
    changed = True
    while changed:
        changed = False
        rows = await _fetchall(
            "SELECT id, source_match1_id, source_match2_id, player1_id, player2_id, status "
            "FROM tournament_matches WHERE tournament_id=? ORDER BY round_no ASC, id ASC",
            (tournament_id,),
        )

        async with aiosqlite.connect(DB_PATH) as db:
            for mid, src1, src2, p1, p2, status in rows:
                if src1 is None and src2 is None:
                    continue
                if status == 'completed':
                    continue

                left = None
                right = None

                if src1:
                    async with db.execute(
                        "SELECT winner_id, status FROM tournament_matches WHERE id=?",
                        (src1,),
                    ) as cur:
                        left = await cur.fetchone()

                if src2:
                    async with db.execute(
                        "SELECT winner_id, status FROM tournament_matches WHERE id=?",
                        (src2,),
                    ) as cur:
                        right = await cur.fetchone()

                w1 = left[0] if left else None
                s1 = left[1] if left else None
                w2 = right[0] if right else None
                s2 = right[1] if right else None

                if s1 == 'completed' and p1 != w1:
                    await db.execute(
                        "UPDATE tournament_matches SET player1_id=?, updated_at=? WHERE id=?",
                        (w1, datetime.utcnow().isoformat(), mid),
                    )
                    changed = True

                if s2 == 'completed' and p2 != w2:
                    await db.execute(
                        "UPDATE tournament_matches SET player2_id=?, updated_at=? WHERE id=?",
                        (w2, datetime.utcnow().isoformat(), mid),
                    )
                    changed = True

                if s1 == 'completed' and s2 == 'completed':
                    if w1 and w2:
                        await db.execute(
                            "UPDATE tournament_matches SET status='scheduled', updated_at=? WHERE id=?",
                            (datetime.utcnow().isoformat(), mid),
                        )
                    else:
                        auto_winner = w1 or w2
                        await db.execute(
                            "UPDATE tournament_matches SET winner_id=?, status='completed', updated_at=? WHERE id=?",
                            (auto_winner, datetime.utcnow().isoformat(), mid),
                        )
                        changed = True

            await db.commit()


async def _create_league_matches(bot: Bot, tournament_id: int, player_ids: list[int], t: dict[str, Any]):
    players = player_ids[:]
    if len(players) % 2 == 1:
        players.append(None)
    n = len(players)
    slots = _build_slots(
        _parse_date(t['start_date']),
        _parse_date(t['end_date']),
        t['match_days'],
        t['match_times'],
        t.get('games_per_day', 999),
    )
    slot_iter = iter(slots)
    rounds = []
    arr = players[:]
    for _ in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a = arr[i]
            b = arr[n - 1 - i]
            if a is not None and b is not None:
                pairs.append((a, b))
        rounds.append(pairs)
        arr = [arr[0]] + [arr[-1]] + arr[1:-1]

    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        for uid in player_ids:
            await db.execute(
                "INSERT OR IGNORE INTO tournament_standings(tournament_id, user_id) VALUES(?,?)",
                (tournament_id, uid),
            )
        for ridx, pairs in enumerate(rounds, start=1):
            for pos, (p1, p2) in enumerate(pairs):
                await db.execute(
                    """
                    INSERT INTO tournament_matches(
                        tournament_id, round_no, round_name, bracket_position, scheduled_at, best_of,
                        player1_id, player2_id, status, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (tournament_id, ridx, f'Тур {ridx}', pos, next(slot_iter, None), 1, p1, p2, 'scheduled', now, now),
                )
        await db.commit()
    await _assign_judges_and_notify(bot, tournament_id)


async def generate_tournament_matches(bot: Bot, tournament_id: int) -> tuple[bool, str]:
    t = await get_tournament(tournament_id)
    if not t:
        return False, 'Турнир не найден.'
    if t['status'] not in ('approved', 'active'):
        return False, 'Турнир не в статусе approved.'
    exists = await _fetchone("SELECT 1 FROM tournament_matches WHERE tournament_id=? LIMIT 1", (tournament_id,))
    if exists:
        return False, 'Матчи уже сгенерированы.'
    player_ids = await get_tournament_players(tournament_id)
    if len(player_ids) < 2:
        return False, 'Нужно минимум 2 игрока.'

    slots = _build_slots(
        _parse_date(t['start_date']),
        _parse_date(t['end_date']),
        t['match_days'],
        t['match_times'],
        t.get('games_per_day', 999),
    )
    if not slots and t['format_type'] == 'league':
        return False, 'Нет доступных слотов для матчей.'

    if t['format_type'] == 'playoff':
        await _create_playoff_matches(bot, tournament_id, player_ids, t)
    else:
        await _create_league_matches(bot, tournament_id, player_ids, t)
    await set_tournament_status(tournament_id, 'active')
    await _notify_players_about_generated_matches(bot, tournament_id)
    return True, 'Матчи сгенерированы.'


async def _notify_players_about_generated_matches(bot: Bot, tournament_id: int):
    rows = await _fetchall(
        "SELECT id, round_name, scheduled_at, player1_id, player2_id, judge_id FROM tournament_matches WHERE tournament_id=? AND status='scheduled' ORDER BY round_no, bracket_position",
        (tournament_id,),
    )
    for mid, round_name, scheduled_at, p1, p2, judge_id in rows:
        if not p1 or not p2:
            continue
        p1n = await _player_label(p1)
        p2n = await _player_label(p2)
        text = f"🏟 Турнирный матч #{mid}\n{round_name}\n{p1n} vs {p2n}\nКогда: {(scheduled_at or 'TBD').replace('T', ' ')}"
        if judge_id:
            text += f"\nСудья: {judge_id}"
        for uid in [p1, p2]:
            try:
                await bot.send_message(uid, text)
            except Exception:
                pass


async def finalize_tournament_match(bot: Bot, match_id: int, winner_id: int, screenshot_file_id: Optional[str] = None):
    row = await get_match(match_id)
    if not row:
        return False, 'Матч не найден.'
    _, tournament_id, _, _, _, _, _, _, p1, p2, _, _, status, _, _, _ = row
    if status == 'completed':
        return False, 'Матч уже закрыт.'
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tournament_matches SET winner_id=?, status='completed', screenshot_file_id=COALESCE(?, screenshot_file_id), updated_at=? WHERE id=?",
            (winner_id, screenshot_file_id, datetime.utcnow().isoformat(), match_id),
        )
        if (await get_tournament(tournament_id))['format_type'] == 'league':
            loser_id = p2 if winner_id == p1 else p1
            await db.execute(
                "UPDATE tournament_standings SET played=played+1, wins=wins+1, points=points+3 WHERE tournament_id=? AND user_id=?",
                (tournament_id, winner_id),
            )
            await db.execute(
                "UPDATE tournament_standings SET played=played+1, losses=losses+1 WHERE tournament_id=? AND user_id=?",
                (tournament_id, loser_id),
            )
        await db.commit()

    t = await get_tournament(tournament_id)
    if t and t['format_type'] == 'playoff':
        await _refresh_playoff_ready_matches(tournament_id)
        await _assign_judges_and_notify(bot, tournament_id)
        final_row = await _fetchone(
            "SELECT winner_id FROM tournament_matches WHERE tournament_id=? AND round_name='Финал' AND status='completed' LIMIT 1",
            (tournament_id,),
        )
        if final_row and final_row[0]:
            await set_tournament_status(tournament_id, 'finished')
            winner_name = await _player_label(final_row[0])
            await _broadcast_tournament(bot, tournament_id, f"🏁 Турнир завершён!\nПобедитель: {winner_name}")
    else:
        remaining = await _fetchone("SELECT COUNT(*) FROM tournament_matches WHERE tournament_id=? AND status!='completed'", (tournament_id,))
        if remaining and int(remaining[0]) == 0:
            top = await _fetchone(
                "SELECT user_id FROM tournament_standings WHERE tournament_id=? ORDER BY points DESC, wins DESC LIMIT 1",
                (tournament_id,),
            )
            await set_tournament_status(tournament_id, 'finished')
            winner_name = await _player_label(top[0]) if top else '—'
            await _broadcast_tournament(bot, tournament_id, f"🏁 Турнир завершён!\nПобедитель: {winner_name}")

    await _broadcast_match_result(bot, match_id)
    return True, 'Результат принят.'


async def _broadcast_match_result(bot: Bot, match_id: int):
    row = await get_match(match_id)
    if not row:
        return
    _, tournament_id, _, round_name, _, _, _, judge_id, p1, p2, _, _, _, winner_id, _, _ = row
    p1n = await _player_label(p1)
    p2n = await _player_label(p2)
    wn = await _player_label(winner_id)
    text = f"✅ Результат турнира\n{round_name}\n{p1n} vs {p2n}\nПобедитель: {wn}"
    users = set(await get_tournament_players(tournament_id))
    if judge_id:
        users.add(judge_id)
    for uid in users:
        try:
            await bot.send_message(uid, text)
        except Exception:
            pass


async def _broadcast_tournament(bot: Bot, tournament_id: int, text: str):
    users = set(await get_tournament_players(tournament_id))
    t = await get_tournament(tournament_id)
    if t:
        users.add(t['creator_id'])
    for uid in users:
        try:
            await bot.send_message(uid, text)
        except Exception:
            pass


async def _matches_text(tournament_id: int) -> str:
    rows = await _fetchall(
        "SELECT id, round_name, scheduled_at, player1_id, player2_id, status, winner_id, judge_id FROM tournament_matches WHERE tournament_id=? ORDER BY round_no, bracket_position",
        (tournament_id,),
    )
    if not rows:
        return 'Матчи ещё не сгенерированы.'
    lines = [f'📋 Матчи турнира #{tournament_id}']
    for mid, round_name, scheduled_at, p1, p2, status, winner_id, judge_id in rows[:60]:
        p1n = await _player_label(p1)
        p2n = await _player_label(p2)
        line = f"#{mid} {round_name}: {p1n} vs {p2n} | {status}"
        if scheduled_at:
            line += f" | {(scheduled_at or '').replace('T', ' ')}"
        if judge_id:
            line += f" | judge {judge_id}"
        if winner_id:
            line += f" | winner {await _player_label(winner_id)}"
        lines.append(line)
    return "\n".join(lines)


async def _players_text(tournament_id: int) -> str:
    ids = await get_tournament_players(tournament_id)
    if not ids:
        return 'Пока никто не зарегистрирован.'
    names = []
    for i, uid in enumerate(ids, start=1):
        names.append(f"{i}. {await _player_label(uid)}")
    return "👥 Участники\n" + "\n".join(names)


async def handle_tournament_photo(message: Message, state: FSMContext, bot: Bot) -> bool:
    if await state.get_state() != JudgeResultState.waiting_photo.state:
        return False
    data = await state.get_data()
    match_id = data.get('judge_match_id')
    if not match_id or not message.photo:
        return False
    row = await get_match(match_id)
    if not row:
        await message.answer('Матч не найден.')
        await state.clear()
        return True
    _, _, _, _, _, _, _, judge_id, p1, p2, _, _, status, _, _, _ = row
    if judge_id != message.from_user.id:
        await message.answer('Этот результат ждётся от назначенного судьи.')
        return True
    if status != 'scheduled':
        await message.answer('Матч уже закрыт или ещё не готов.')
        await state.clear()
        return True
    file_id = message.photo[-1].file_id
    await state.update_data(judge_file_id=file_id)
    p1n = await _player_label(p1)
    p2n = await _player_label(p2)
    await message.answer('Скрин получен. Кто победил?', reply_markup=_winner_pick_kb(match_id, p1n, p2n))
    return True


def register_tournament_handlers(dp: Dispatcher, bot: Bot, operators: set[int], get_keyboard_for_user: Callable[[int], Any]):
    global CTX
    CTX = TournamentContext(operators=set(operators), get_keyboard_for_user=get_keyboard_for_user)

    @dp.message(JudgeResultState.waiting_photo, F.photo)
    async def tournament_judge_photo(message: Message, state: FSMContext):
        await handle_tournament_photo(message, state, bot)

    @dp.message(F.text == '🏆 Create tournament')
    async def tournament_create_start(message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(TournamentCreate.title)
        await message.answer('Введите название турнира.')

    @dp.message(F.text == '📋 Tournaments')
    async def tournament_list_btn(message: Message):
        items = await list_tournaments(statuses=['pending', 'approved', 'active', 'finished'], limit=15)
        if not items:
            await message.answer('Турниров пока нет.')
            return
        for t in items:
            full = await get_tournament(t['id'])
            await message.answer(await _tournament_text(full), reply_markup=_tournament_card_kb(full, message.from_user.id))

    @dp.message(F.text == '⚖️ My judge matches')
    async def my_judge_matches(message: Message):
        rows = await get_open_matches_for_judge(message.from_user.id)
        if not rows:
            await message.answer('Открытых матчей судьи нет.')
            return
        for mid, tid, round_name, scheduled_at, p1, p2 in rows:
            await message.answer(
                f"⚖️ Матч #{mid} турнира #{tid}\n{round_name}\n{await _player_label(p1)} vs {await _player_label(p2)}\nКогда: {(scheduled_at or 'TBD').replace('T', ' ')}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='📸 Отправить скрин результата', callback_data=f'judgeopen|{mid}')]])
            )

    @dp.message(TournamentCreate.title)
    async def tc_title(message: Message, state: FSMContext):
        await state.update_data(title=(message.text or '').strip(), creator_id=message.from_user.id)
        await state.set_state(TournamentCreate.start_date)
        await message.answer('Введите дату начала турнира в формате DD.MM.YYYY')

    @dp.message(TournamentCreate.start_date)
    async def tc_start(message: Message, state: FSMContext):
        try:
            start_d = _parse_date(message.text or '')
        except Exception:
            await message.answer('Неверная дата. Пример: 15.04.2026')
            return
        await state.update_data(start_date=start_d.strftime(DATE_FMT))
        await state.set_state(TournamentCreate.end_date)
        await message.answer('Введите дату окончания турнира в формате DD.MM.YYYY')

    @dp.message(TournamentCreate.end_date)
    async def tc_end(message: Message, state: FSMContext):
        try:
            end_d = _parse_date(message.text or '')
        except Exception:
            await message.answer('Неверная дата. Пример: 30.04.2026')
            return
        data = await state.get_data()
        start_d = _parse_date(data['start_date'])
        if end_d < start_d:
            await message.answer('Дата окончания не может быть раньше даты начала.')
            return
        if (end_d - start_d).days > 60:
            await message.answer('Сделай турнир короче: максимум 60 дней.')
            return
        await state.update_data(end_date=end_d.strftime(DATE_FMT))
        await state.set_state(TournamentCreate.match_days)
        await message.answer('По каким дням идут игры?\nПример: Пн Ср Сб')

    @dp.message(TournamentCreate.match_days)
    async def tc_days(message: Message, state: FSMContext):
        try:
            days = _parse_days(message.text or '')
        except Exception as e:
            await message.answer(str(e))
            return
        await state.update_data(match_days=days)
        await state.set_state(TournamentCreate.match_times)
        await message.answer('Во сколько идут матчи?\nПример: 18:00 19:00 20:00')

    @dp.message(TournamentCreate.match_times)
    async def tc_times(message: Message, state: FSMContext):
        try:
            times = _parse_times(message.text or '')
        except Exception:
            await message.answer('Укажи время в формате 18:00 19:00 20:00')
            return
        await state.update_data(match_times=times)
        await state.set_state(TournamentCreate.games_per_day)
        await message.answer('Сколько максимум матчей в день?')

    @dp.message(TournamentCreate.games_per_day)
    async def tc_games_per_day(message: Message, state: FSMContext):
        try:
            games_per_day = int((message.text or '').strip())
        except Exception:
            await message.answer('Нужно число.')
            return
        if games_per_day < 1 or games_per_day > 100:
            await message.answer('Допустимо от 1 до 100 матчей в день.')
            return
        await state.update_data(games_per_day=games_per_day)
        await state.set_state(TournamentCreate.max_players)
        await message.answer('Сколько максимум игроков?')

    @dp.message(TournamentCreate.max_players)
    async def tc_max(message: Message, state: FSMContext):
        try:
            mx = int((message.text or '').strip())
        except Exception:
            await message.answer('Нужно число.')
            return
        if mx < 2 or mx > 256:
            await message.answer('Допустимо от 2 до 256 игроков.')
            return
        await state.update_data(max_players=mx)
        await state.set_state(TournamentCreate.format_type)
        await message.answer('Выбери тип турнира.', reply_markup=_tournament_type_kb())

    @dp.callback_query(F.data.startswith('tourfmt|'))
    async def tc_format(callback: CallbackQuery, state: FSMContext):
        fmt = (callback.data or '').split('|', 1)[1]
        if fmt not in ('playoff', 'league'):
            await callback.answer('Неверный тип')
            return
        await state.update_data(format_type=fmt)
        await callback.answer('Сохранено')
        if fmt == 'playoff':
            await state.set_state(TournamentCreate.semifinal_best_of)
            await callback.message.answer('Сколько игр в полуфинале? Например 3 для BO3')
        else:
            await state.set_state(TournamentCreate.need_judges)
            await callback.message.answer('Нужны судьи?', reply_markup=_yes_no_kb('tourjudges|yes', 'tourjudges|no'))

    @dp.message(TournamentCreate.semifinal_best_of)
    async def tc_semi_bo(message: Message, state: FSMContext):
        try:
            bo = int((message.text or '').strip())
        except Exception:
            await message.answer('Нужно число.')
            return
        if bo < 1 or bo > 9 or bo % 2 == 0:
            await message.answer('Укажи нечётное число от 1 до 9.')
            return
        await state.update_data(semifinal_best_of=bo)
        await state.set_state(TournamentCreate.semifinal_slots)
        await message.answer('Когда проходят полуфиналы?\nПример: 20.04.2026 18:00, 20.04.2026 19:00')

    @dp.message(TournamentCreate.semifinal_slots)
    async def tc_semi_slots(message: Message, state: FSMContext):
        try:
            slots = _parse_slot_pairs(message.text or '')
        except Exception:
            await message.answer('Пример: 20.04.2026 18:00, 20.04.2026 19:00')
            return
        await state.update_data(semifinal_slots=slots)
        await state.set_state(TournamentCreate.final_best_of)
        await message.answer('Сколько игр в финале? Например 5 для BO5')

    @dp.message(TournamentCreate.final_best_of)
    async def tc_final_bo(message: Message, state: FSMContext):
        try:
            bo = int((message.text or '').strip())
        except Exception:
            await message.answer('Нужно число.')
            return
        if bo < 1 or bo > 9 or bo % 2 == 0:
            await message.answer('Укажи нечётное число от 1 до 9.')
            return
        await state.update_data(final_best_of=bo)
        await state.set_state(TournamentCreate.final_slots)
        await message.answer('Когда проходят финалы?\nПример: 22.04.2026 19:00')

    @dp.message(TournamentCreate.final_slots)
    async def tc_final_slots(message: Message, state: FSMContext):
        try:
            slots = _parse_slot_pairs(message.text or '')
        except Exception:
            await message.answer('Пример: 22.04.2026 19:00')
            return
        await state.update_data(final_slots=slots)
        await state.set_state(TournamentCreate.need_judges)
        await message.answer('Нужны судьи?', reply_markup=_yes_no_kb('tourjudges|yes', 'tourjudges|no'))

    @dp.callback_query(F.data.startswith('tourjudges|'))
    async def tc_need_judges(callback: CallbackQuery, state: FSMContext):
        flag = (callback.data or '').split('|', 1)[1]
        await callback.answer('Сохранено')
        if flag == 'yes':
            await state.set_state(TournamentCreate.judges)
            await callback.message.answer('Отправь Telegram user_id судей через запятую.\nПример: 123456789, 987654321')
        else:
            await state.update_data(judges=[])
            await state.set_state(TournamentCreate.prize_pool)
            await callback.message.answer('Укажи призовой фонд в рублях.')

    @dp.message(TournamentCreate.judges)
    async def tc_judges(message: Message, state: FSMContext):
        try:
            judges = _parse_judge_ids(message.text or '')
        except Exception as e:
            await message.answer(str(e))
            return
        await state.update_data(judges=judges)
        await state.set_state(TournamentCreate.prize_pool)
        await message.answer('Укажи призовой фонд в рублях.')

    @dp.message(TournamentCreate.prize_pool)
    async def tc_prize(message: Message, state: FSMContext):
        try:
            prize = int((message.text or '').strip())
        except Exception:
            await message.answer('Нужно число.')
            return
        if prize < 0:
            await message.answer('Призовой фонд не может быть отрицательным.')
            return
        await state.update_data(prize_pool_rub=prize)
        data = await state.get_data()
        await state.set_state(TournamentCreate.confirm)
        await message.answer(_summary_text(data), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='📨 Отправить модератору', callback_data='tourcreate|submit')],
            [InlineKeyboardButton(text='❌ Отмена', callback_data='tourcreate|cancel')],
        ]))

    @dp.callback_query(F.data == 'tourcreate|cancel')
    async def tc_cancel(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.answer('Отменено')
        await callback.message.answer('Создание турнира отменено.')

    @dp.callback_query(F.data == 'tourcreate|submit')
    async def tc_submit(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        tid = await create_tournament(data)
        await state.clear()
        await callback.answer('Отправлено')
        await callback.message.answer(f'✅ Заявка на турнир #{tid} отправлена модератору.')
        t = await get_tournament(tid)
        text = await _tournament_text(t)
        for op in CTX.operators:
            try:
                await bot.send_message(op, text + f"\n\nСоздатель: {callback.from_user.id}", reply_markup=_moderation_kb(tid))
            except Exception:
                pass

    @dp.callback_query(F.data.startswith('tourapprove|'))
    async def tour_approve(callback: CallbackQuery):
        if callback.from_user.id not in CTX.operators:
            await callback.answer('Нет доступа')
            return
        tid = int((callback.data or '').split('|', 1)[1])
        await set_tournament_status(tid, 'approved', moderator_id=callback.from_user.id)
        t = await get_tournament(tid)
        await callback.answer('Одобрено')
        await callback.message.edit_text('✅ Турнир одобрен.')
        try:
            await bot.send_message(t['creator_id'], f'✅ Турнир #{tid} одобрен модератором.')
        except Exception:
            pass

    @dp.callback_query(F.data.startswith('tourreject|'))
    async def tour_reject(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in CTX.operators:
            await callback.answer('Нет доступа')
            return
        tid = int((callback.data or '').split('|', 1)[1])
        await state.set_state(TournamentReject.reason)
        await state.update_data(reject_tournament_id=tid, reject_operator_id=callback.from_user.id)
        await callback.answer()
        await callback.message.answer('Напиши причину отказа одним сообщением.')

    @dp.message(TournamentReject.reason)
    async def tour_reject_reason(message: Message, state: FSMContext):
        data = await state.get_data()
        tid = int(data['reject_tournament_id'])
        op = int(data['reject_operator_id'])
        reason = (message.text or '').strip()
        await set_tournament_status(tid, 'rejected', moderator_id=op, reject_reason=reason)
        t = await get_tournament(tid)
        await state.clear()
        await message.answer(f'❌ Турнир #{tid} отклонён.')
        try:
            await bot.send_message(t['creator_id'], f'❌ Турнир #{tid} отклонён.\nПричина: {reason}')
        except Exception:
            pass

    @dp.callback_query(F.data.startswith('tourreg|'))
    async def tour_register(callback: CallbackQuery):
        tid = int((callback.data or '').split('|', 1)[1])
        t = await get_tournament(tid)
        if not t:
            await callback.answer('Турнир не найден')
            return
        if t['status'] != 'approved':
            await callback.answer('Регистрация закрыта.')
            return
        if await is_registered(tid, callback.from_user.id):
            ok = await unregister_player(tid, callback.from_user.id)
            await callback.answer('Регистрация отменена' if ok else 'Не удалось')
        else:
            ok = await register_player(tid, callback.from_user.id)
            await callback.answer('Зарегистрирован' if ok else 'Не удалось зарегистрировать')
            if ok:
                count = await count_tournament_players(tid)
                if count >= t['max_players']:
                    await set_tournament_status(tid, 'approved')
                    try:
                        await bot.send_message(t['creator_id'], f'ℹ️ Турнир #{tid}: набран максимум игроков ({count}/{t["max_players"]}). Можно закрывать регистрацию и генерировать матчи.')
                    except Exception:
                        pass
        full = await get_tournament(tid)
        await callback.message.edit_text(await _tournament_text(full), reply_markup=_tournament_card_kb(full, callback.from_user.id))

    @dp.callback_query(F.data.startswith('tourmanage|'))
    async def tour_manage(callback: CallbackQuery):
        tid = int((callback.data or '').split('|', 1)[1])
        t = await get_tournament(tid)
        if not t:
            await callback.answer('Турнир не найден')
            return
        if callback.from_user.id != t['creator_id'] and callback.from_user.id not in CTX.operators:
            await callback.answer('Нет доступа')
            return
        await callback.answer()
        await callback.message.answer('⚙️ Управление турниром', reply_markup=_manage_kb(tid))

    @dp.callback_query(F.data.startswith('tourplayers|'))
    async def tour_players(callback: CallbackQuery):
        tid = int((callback.data or '').split('|', 1)[1])
        await callback.answer()
        await callback.message.answer(await _players_text(tid))

    @dp.callback_query(F.data.startswith('tourmatches|'))
    async def tour_matches(callback: CallbackQuery):
        tid = int((callback.data or '').split('|', 1)[1])
        await callback.answer()
        await callback.message.answer(await _matches_text(tid))

    @dp.callback_query(F.data.startswith('tourstart|'))
    async def tour_start(callback: CallbackQuery):
        tid = int((callback.data or '').split('|', 1)[1])
        t = await get_tournament(tid)
        if not t:
            await callback.answer('Турнир не найден')
            return
        if callback.from_user.id != t['creator_id'] and callback.from_user.id not in CTX.operators:
            await callback.answer('Нет доступа')
            return
        ok, msg = await generate_tournament_matches(bot, tid)
        await callback.answer(msg)
        await callback.message.answer(msg)

    @dp.callback_query(F.data.startswith('judgeopen|'))
    async def judge_open(callback: CallbackQuery, state: FSMContext):
        mid = int((callback.data or '').split('|', 1)[1])
        row = await get_match(mid)
        if not row:
            await callback.answer('Матч не найден')
            return
        _, _, _, _, _, _, _, judge_id, _, _, _, _, status, _, _, _ = row
        if judge_id != callback.from_user.id:
            await callback.answer('Ты не назначен судьёй на этот матч.')
            return
        if status != 'scheduled':
            await callback.answer('Матч пока не готов или уже закрыт.')
            return
        await state.set_state(JudgeResultState.waiting_photo)
        await state.update_data(judge_match_id=mid)
        await callback.answer('Жду фото')
        await callback.message.answer(f'Отправь скрин результата для матча #{mid}.')

    @dp.callback_query(F.data.startswith('judgewin|'))
    async def judge_pick_winner(callback: CallbackQuery, state: FSMContext):
        _, mid_s, side = (callback.data or '').split('|', 2)
        mid = int(mid_s)
        row = await get_match(mid)
        if not row:
            await callback.answer('Матч не найден')
            return
        _, _, _, _, _, _, _, judge_id, p1, p2, _, _, status, _, _, _ = row
        if judge_id != callback.from_user.id:
            await callback.answer('Ты не судья этого матча.')
            return
        if status != 'scheduled':
            await callback.answer('Матч уже закрыт.')
            return
        data = await state.get_data()
        screenshot_file_id = data.get('judge_file_id')
        winner_id = p1 if side == '1' else p2
        ok, msg = await finalize_tournament_match(bot, mid, winner_id, screenshot_file_id=screenshot_file_id)
        if ok:
            await state.clear()
            await callback.message.edit_text(f'✅ Результат матча #{mid} принят.')
        await callback.answer(msg)

    @dp.callback_query(F.data == 'noop')
    async def noop(callback: CallbackQuery):
        await callback.answer()
