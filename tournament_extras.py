from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any, Callable, Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database import DB_PATH, get_user_name, get_user_id_by_name, save_user_name, get_user_language

NICK_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_.\- ]{3,24}$")
SYNC_TASK: asyncio.Task | None = None
MAX_REPLACEMENTS_PER_MATCH = 2


class ExtraState(StatesGroup):
    waiting_replace_payload = State()
    waiting_nick = State()
    waiting_deputy = State()
    waiting_reject_reason = State()


async def init_tournament_extras_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tournament_replacement_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                match_id INTEGER NOT NULL,
                out_user_id INTEGER NOT NULL,
                in_user_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_by INTEGER NOT NULL,
                judge_id INTEGER,
                created_at TEXT NOT NULL,
                decided_at TEXT,
                reject_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS tournament_nickname_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                requested_nickname TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_by INTEGER NOT NULL,
                judge_id INTEGER,
                created_at TEXT NOT NULL,
                decided_at TEXT,
                reject_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS tournament_sync_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                job_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tournament_action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                actor_user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        try:
            await db.execute("ALTER TABLE tournaments ADD COLUMN deputy_founder_id INTEGER")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE tournaments ADD COLUMN deputy_scope_json TEXT NOT NULL DEFAULT '[]'")
        except Exception:
            pass
        await db.commit()


async def _fetchone(sql: str, params: tuple = ()) -> Optional[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(sql, params) as cur:
            return await cur.fetchone()


async def _fetchall(sql: str, params: tuple = ()) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(sql, params) as cur:
            return await cur.fetchall()


def _json_load(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


async def _tournament_core(tournament_id: int) -> Optional[dict[str, Any]]:
    row = await _fetchone(
        "SELECT id, creator_id, judges_json, deputy_founder_id, deputy_scope_json, status FROM tournaments WHERE id=?",
        (tournament_id,),
    )
    if not row:
        return None
    return {
        "id": row[0],
        "creator_id": row[1],
        "judges": _json_load(row[2], []),
        "deputy_founder_id": row[3],
        "deputy_scope": _json_load(row[4], []),
        "status": row[5],
    }


def _has_scope(t: dict[str, Any], user_id: int, operators: set[int], scope: str) -> bool:
    if user_id in operators or user_id == t["creator_id"]:
        return True
    if user_id != t.get("deputy_founder_id"):
        return False
    scopes = set(t.get("deputy_scope") or [])
    return "all" in scopes or scope in scopes


def _is_judge(t: dict[str, Any], user_id: int, operators: set[int]) -> bool:
    return user_id in operators or user_id == t["creator_id"] or user_id in set(t.get("judges") or [])


async def _log_action(
    tournament_id: int,
    actor_user_id: int,
    action: str,
    entity_type: str,
    entity_id: Optional[int],
    details: dict[str, Any],
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO tournament_action_log(tournament_id, actor_user_id, action, entity_type, entity_id, details_json, created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                tournament_id,
                actor_user_id,
                action,
                entity_type,
                entity_id,
                json.dumps(details, ensure_ascii=False),
                datetime.utcnow().isoformat(),
            ),
        )
        await db.commit()


async def _queue_sync(tournament_id: int, job_type: str, payload: dict[str, Any]) -> None:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO tournament_sync_jobs(tournament_id, job_type, payload_json, status, attempts, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (tournament_id, job_type, json.dumps(payload, ensure_ascii=False), "pending", 0, now, now),
        )
        await db.commit()


async def _sync_recipients(tournament_id: int) -> set[int]:
    t = await _tournament_core(tournament_id)
    if not t:
        return set()
    users = {t["creator_id"]}
    users.update(set(t.get("judges") or []))
    if t.get("deputy_founder_id"):
        users.add(t["deputy_founder_id"])
    rows = await _fetchall("SELECT user_id FROM tournament_players WHERE tournament_id=?", (tournament_id,))
    users.update({r[0] for r in rows})
    return users


async def run_tournament_sync_once(bot: Bot) -> None:
    row = await _fetchone(
        "SELECT id, tournament_id, payload_json FROM tournament_sync_jobs WHERE status='pending' ORDER BY id ASC LIMIT 1"
    )
    if not row:
        return
    job_id, tournament_id, payload_json = row
    payload = _json_load(payload_json, {})
    text = str(payload.get("text") or "Tournament update").strip()
    ok = True
    err = ""
    try:
        for uid in await _sync_recipients(int(tournament_id)):
            try:
                await bot.send_message(uid, text)
            except Exception:
                pass
    except Exception as e:
        ok = False
        err = f"{type(e).__name__}: {e}"
    async with aiosqlite.connect(DB_PATH) as db:
        if ok:
            await db.execute(
                "UPDATE tournament_sync_jobs SET status='done', attempts=attempts+1, updated_at=? WHERE id=?",
                (datetime.utcnow().isoformat(), job_id),
            )
        else:
            await db.execute(
                "UPDATE tournament_sync_jobs SET attempts=attempts+1, last_error=?, updated_at=? WHERE id=?",
                (err[:500], datetime.utcnow().isoformat(), job_id),
            )
        await db.commit()


def start_tournament_sync_worker(bot: Bot) -> None:
    global SYNC_TASK
    if SYNC_TASK and not SYNC_TASK.done():
        return

    async def _worker() -> None:
        while True:
            try:
                await run_tournament_sync_once(bot)
            except Exception:
                pass
            await asyncio.sleep(10)

    SYNC_TASK = asyncio.create_task(_worker())

async def _lang(user_id: int) -> str:
    lang = await get_user_language(user_id)
    return "en" if lang == "en" else "ru"


def _tr(lang: str, en: str, ru: str) -> str:
    return en if lang == "en" else ru


def _menu_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_tr(lang, "🧾 My tournament matches", "🧾 Мои турнирные матчи"), callback_data="xmenu|mymatches")],
            [InlineKeyboardButton(text=_tr(lang, "🪪 Nick check request", "🪪 Заявка на проверку ника"), callback_data="xmenu|nickcheck")],
            [InlineKeyboardButton(text=_tr(lang, "⚖️ Judge panel", "⚖️ Панель судьи"), callback_data="xmenu|judgepanel")],
            [InlineKeyboardButton(text=_tr(lang, "👤 Set deputy founder", "👤 Назначить зама основателя"), callback_data="xmenu|deputy")],
        ]
    )


def _replacement_kb(match_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=_tr(lang, "🔁 Replacement request", "🔁 Заявка на замену"), callback_data=f"xrepreq|{match_id}")]]
    )


def _replacement_review_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Approve replacement", callback_data=f"xrepok|{req_id}")],
            [InlineKeyboardButton(text="❌ Reject replacement", callback_data=f"xrepno|{req_id}")],
        ]
    )


def _nick_review_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Approve nick", callback_data=f"xnickok|{req_id}")],
            [InlineKeyboardButton(text="❌ Reject nick", callback_data=f"xnickno|{req_id}")],
        ]
    )


async def _notify_judges(bot: Bot, tournament_id: int, text: str, markup: InlineKeyboardMarkup) -> None:
    t = await _tournament_core(tournament_id)
    if not t:
        return
    recipients = set(t.get("judges") or [])
    recipients.add(t["creator_id"])
    if t.get("deputy_founder_id"):
        recipients.add(t["deputy_founder_id"])
    for uid in recipients:
        try:
            await bot.send_message(uid, text, reply_markup=markup)
        except Exception:
            pass


def register_tournament_extra_handlers(
    dp: Dispatcher,
    bot: Bot,
    operators: set[int],
    get_keyboard_for_user: Callable[[int], Any],
) -> None:
    ops = set(operators)

    @dp.message(lambda m: (m.text or "").strip() in {"🏟 Tournament menu", "🏟 Меню турниров"})
    async def tournament_menu(message: Message) -> None:
        lang = await _lang(message.from_user.id)
        await message.answer(_tr(lang, "Tournament tools", "Инструменты турниров"), reply_markup=_menu_kb(lang))

    @dp.message(lambda m: (m.text or "").strip() in {"⚖️ Judge panel", "⚖️ Панель судьи"})
    async def open_judge_panel_button(message: Message) -> None:
        lang = await _lang(message.from_user.id)
        await message.answer(
            _tr(lang, "Judge panel", "Панель судьи"),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=_tr(lang, "Open requests", "Открыть заявки"), callback_data="xmenu|judgepanel")]]
            ),
        )

    @dp.callback_query(F.data == "xmenu|mymatches")
    async def my_tournament_matches(callback: CallbackQuery) -> None:
        uid = callback.from_user.id
        lang = await _lang(uid)
        rows = await _fetchall(
            """
            SELECT id, tournament_id, round_name, scheduled_at, player1_id, player2_id
            FROM tournament_matches
            WHERE status='scheduled' AND (player1_id=? OR player2_id=?)
            ORDER BY scheduled_at ASC, id ASC
            LIMIT 20
            """,
            (uid, uid),
        )
        if not rows:
            await callback.answer(_tr(lang, "No active tournament matches", "Нет активных турнирных матчей"), show_alert=True)
            return
        await callback.answer()
        for mid, tid, round_name, scheduled_at, p1, p2 in rows:
            p1n = await get_user_name(p1) or str(p1)
            p2n = await get_user_name(p2) or str(p2)
            await callback.message.answer(
                _tr(
                    lang,
                    f"Match #{mid} (Tournament #{tid})\n{round_name}\n{p1n} vs {p2n}\nWhen: {(scheduled_at or 'TBD').replace('T', ' ')}",
                    f"Матч #{mid} (Турнир #{tid})\n{round_name}\n{p1n} vs {p2n}\nКогда: {(scheduled_at or 'TBD').replace('T', ' ')}",
                ),
                reply_markup=_replacement_kb(mid, lang),
            )

    @dp.callback_query(F.data.startswith("xrepreq|"))
    async def replacement_request_start(callback: CallbackQuery, state: FSMContext) -> None:
        lang = await _lang(callback.from_user.id)
        mid = int((callback.data or "").split("|", 1)[1])
        row = await _fetchone(
            "SELECT tournament_id, player1_id, player2_id, status FROM tournament_matches WHERE id=?",
            (mid,),
        )
        if not row:
            await callback.answer(_tr(lang, "Match not found", "Матч не найден"), show_alert=True)
            return
        tid, p1, p2, status = row
        if status != "scheduled" or callback.from_user.id not in (p1, p2):
            await callback.answer(_tr(lang, "Only match players can request replacement", "Только игроки матча могут запросить замену"), show_alert=True)
            return
        await state.set_state(ExtraState.waiting_replace_payload)
        await state.update_data(replace_match_id=mid, replace_tournament_id=tid)
        await callback.answer()
        await callback.message.answer(_tr(
            lang,
            "Send in one message: out_user_id, in_user_id, reason\nExample:\n123456789, 987654321, internet disconnect",
            "Отправь одним сообщением: out_user_id, in_user_id, reason\nПример:\n123456789, 987654321, отключился интернет",
        ))

    @dp.message(ExtraState.waiting_replace_payload)
    async def replacement_request_submit(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        mid = int(data["replace_match_id"])
        tid = int(data["replace_tournament_id"])
        text = (message.text or "").strip()
        parts = [p.strip() for p in text.split(",", 2)]
        if len(parts) != 3:
            await message.answer("Wrong format. Need: out_user_id, in_user_id, reason")
            return
        try:
            out_uid = int(parts[0])
            in_uid = int(parts[1])
        except Exception:
            await message.answer("out_user_id and in_user_id must be numbers")
            return
        reason = parts[2][:500]
        mrow = await _fetchone(
            "SELECT player1_id, player2_id, status, scheduled_at FROM tournament_matches WHERE id=?",
            (mid,),
        )
        if not mrow or mrow[2] != "scheduled":
            await message.answer("Match is no longer active")
            await state.clear()
            return
        p1, p2, _, scheduled_at = mrow
        if scheduled_at:
            try:
                if datetime.utcnow() >= datetime.fromisoformat(str(scheduled_at)):
                    await message.answer("Replacement is forbidden after match start time")
                    return
            except ValueError:
                pass
        rep_count = await _fetchone(
            "SELECT COUNT(*) FROM tournament_replacement_requests WHERE match_id=? AND status='approved'",
            (mid,),
        )
        if rep_count and int(rep_count[0]) >= MAX_REPLACEMENTS_PER_MATCH:
            await message.answer("Replacement limit for this match has been reached")
            return
        if out_uid not in (p1, p2):
            await message.answer("out_user_id must be one of current match players")
            return
        if in_uid in (p1, p2) or in_uid == out_uid:
            await message.answer("in_user_id must be another player")
            return
        reg = await _fetchone(
            "SELECT 1 FROM tournament_players WHERE tournament_id=? AND user_id=?",
            (tid, in_uid),
        )
        if not reg:
            await message.answer("Replacement player is not registered in this tournament")
            return
        busy = await _fetchone(
            """
            SELECT 1 FROM tournament_matches
            WHERE tournament_id=? AND status='scheduled' AND id != ?
              AND (player1_id=? OR player2_id=?)
            LIMIT 1
            """,
            (tid, mid, in_uid, in_uid),
        )
        if busy:
            await message.answer("Replacement player is already assigned to another active match")
            return
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """
                INSERT INTO tournament_replacement_requests(
                    tournament_id, match_id, out_user_id, in_user_id, reason, status, created_by, created_at
                ) VALUES(?,?,?,?,?,'open',?,?)
                """,
                (tid, mid, out_uid, in_uid, reason, message.from_user.id, now),
            )
            req_id = int(cur.lastrowid or 0)
            await db.commit()
        await _log_action(
            tid,
            message.from_user.id,
            "replacement_request_create",
            "tournament_replacement_requests",
            req_id,
            {"match_id": mid, "out_user_id": out_uid, "in_user_id": in_uid, "reason": reason},
        )
        await message.answer("Replacement request created and sent to judges")
        await _notify_judges(
            bot,
            tid,
            f"Replacement request #{req_id}\nTournament #{tid}, match #{mid}\nout: {out_uid}\nin: {in_uid}\nreason: {reason}",
            _replacement_review_kb(req_id),
        )
        await state.clear()

    @dp.callback_query(F.data.startswith("xrepok|"))
    async def replacement_approve(callback: CallbackQuery) -> None:
        req_id = int((callback.data or "").split("|", 1)[1])
        row = await _fetchone(
            """
            SELECT id, tournament_id, match_id, out_user_id, in_user_id, reason, status, created_by
            FROM tournament_replacement_requests
            WHERE id=?
            """,
            (req_id,),
        )
        if not row:
            await callback.answer("Request not found", show_alert=True)
            return
        _, tid, mid, out_uid, in_uid, reason, status, created_by = row
        t = await _tournament_core(tid)
        if not t or not _is_judge(t, callback.from_user.id, ops):
            await callback.answer("Only judge can approve", show_alert=True)
            return
        if status != "open":
            await callback.answer("Request already decided", show_alert=True)
            return
        mrow = await _fetchone("SELECT player1_id, player2_id, status FROM tournament_matches WHERE id=?", (mid,))
        if not mrow or mrow[2] != "scheduled":
            await callback.answer("Match not active", show_alert=True)
            return
        p1, p2, _ = mrow
        if out_uid != p1 and out_uid != p2:
            await callback.answer("out_user_id is not in match anymore", show_alert=True)
            return
        async with aiosqlite.connect(DB_PATH) as db:
            if out_uid == p1:
                await db.execute(
                    "UPDATE tournament_matches SET player1_id=?, updated_at=? WHERE id=?",
                    (in_uid, datetime.utcnow().isoformat(), mid),
                )
            else:
                await db.execute(
                    "UPDATE tournament_matches SET player2_id=?, updated_at=? WHERE id=?",
                    (in_uid, datetime.utcnow().isoformat(), mid),
                )
            await db.execute(
                "UPDATE tournament_replacement_requests SET status='approved', judge_id=?, decided_at=? WHERE id=?",
                (callback.from_user.id, datetime.utcnow().isoformat(), req_id),
            )
            await db.commit()
        await _log_action(
            tid,
            callback.from_user.id,
            "replacement_request_approve",
            "tournament_replacement_requests",
            req_id,
            {"match_id": mid, "out_user_id": out_uid, "in_user_id": in_uid, "reason": reason},
        )
        await _queue_sync(
            tid,
            "replacement_approved",
            {"text": f"Judge approved replacement in match #{mid}: {out_uid} -> {in_uid}"},
        )
        await callback.answer("Approved")
        await callback.message.edit_text(f"✅ Replacement request #{req_id} approved")
        try:
            await bot.send_message(created_by, f"✅ Your replacement request #{req_id} was approved")
        except Exception:
            pass

    @dp.callback_query(F.data.startswith("xrepno|"))
    async def replacement_reject_start(callback: CallbackQuery, state: FSMContext) -> None:
        req_id = int((callback.data or "").split("|", 1)[1])
        row = await _fetchone("SELECT tournament_id, status FROM tournament_replacement_requests WHERE id=?", (req_id,))
        if not row:
            await callback.answer("Request not found", show_alert=True)
            return
        tid, status = row
        t = await _tournament_core(tid)
        if not t or not _is_judge(t, callback.from_user.id, ops):
            await callback.answer("Only judge can reject", show_alert=True)
            return
        if status != "open":
            await callback.answer("Already decided", show_alert=True)
            return
        await state.set_state(ExtraState.waiting_reject_reason)
        await state.update_data(reject_type="replacement", reject_req_id=req_id, reject_tid=tid)
        await callback.answer()
        await callback.message.answer("Send reject reason in one message")

    @dp.callback_query(F.data == "xmenu|nickcheck")
    async def nickcheck_entry(callback: CallbackQuery, state: FSMContext) -> None:
        lang = await _lang(callback.from_user.id)
        await state.set_state(ExtraState.waiting_nick)
        await callback.answer()
        await callback.message.answer(_tr(lang, "Send: tournament_id, new_nickname", "Отправь: tournament_id, new_nickname"))

    @dp.callback_query(F.data == "xmenu|deputy")
    async def deputy_entry(callback: CallbackQuery, state: FSMContext) -> None:
        lang = await _lang(callback.from_user.id)
        await state.set_state(ExtraState.waiting_deputy)
        await callback.answer()
        await callback.message.answer(_tr(lang, "Send: tournament_id, deputy_user_id", "Отправь: tournament_id, deputy_user_id"))

    @dp.message(ExtraState.waiting_nick)
    async def nickcheck_submit(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        parts = [p.strip() for p in text.split(",", 1)]
        if len(parts) != 2:
            await message.answer("Wrong format. Need: tournament_id, new_nickname")
            return
        try:
            tid = int(parts[0])
        except Exception:
            await message.answer("tournament_id must be number")
            return
        nickname = parts[1]
        if not NICK_RE.fullmatch(nickname):
            await message.answer("Nickname must be 3-24 chars: letters, digits, _, -, dot, space")
            return
        t = await _tournament_core(tid)
        if not t:
            await message.answer("Tournament not found")
            return
        reg = await _fetchone(
            "SELECT 1 FROM tournament_players WHERE tournament_id=? AND user_id=?",
            (tid, message.from_user.id),
        )
        if not reg and not _has_scope(t, message.from_user.id, ops, "manage_tournament"):
            await message.answer("You are not participant of this tournament")
            return
        exists = await get_user_id_by_name(nickname)
        if exists and exists != message.from_user.id:
            await message.answer("Nickname already taken by another user")
            return
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """
                INSERT INTO tournament_nickname_checks(
                    tournament_id, user_id, requested_nickname, status, created_by, created_at
                ) VALUES(?,?,?,'open',?,?)
                """,
                (tid, message.from_user.id, nickname, message.from_user.id, now),
            )
            req_id = int(cur.lastrowid or 0)
            await db.commit()
        await _log_action(
            tid,
            message.from_user.id,
            "nickname_check_create",
            "tournament_nickname_checks",
            req_id,
            {"requested_nickname": nickname},
        )
        await message.answer("Nick check request created and sent to judges")
        await _notify_judges(
            bot,
            tid,
            f"Nickname check request #{req_id}\nuser: {message.from_user.id}\nnickname: {nickname}",
            _nick_review_kb(req_id),
        )
        await state.clear()

    @dp.callback_query(F.data.startswith("xnickok|"))
    async def nickcheck_approve(callback: CallbackQuery) -> None:
        req_id = int((callback.data or "").split("|", 1)[1])
        row = await _fetchone(
            """
            SELECT id, tournament_id, user_id, requested_nickname, status, created_by
            FROM tournament_nickname_checks
            WHERE id=?
            """,
            (req_id,),
        )
        if not row:
            await callback.answer("Request not found", show_alert=True)
            return
        _, tid, user_id, nickname, status, created_by = row
        t = await _tournament_core(tid)
        if not t or not _is_judge(t, callback.from_user.id, ops):
            await callback.answer("Only judge can approve", show_alert=True)
            return
        if status != "open":
            await callback.answer("Already decided", show_alert=True)
            return
        exists = await get_user_id_by_name(nickname)
        if exists and exists != user_id:
            await callback.answer("Nickname already taken now", show_alert=True)
            return
        ok = await save_user_name(user_id, nickname)
        if not ok:
            await callback.answer("Failed to save nickname", show_alert=True)
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE tournament_nickname_checks SET status='approved', judge_id=?, decided_at=? WHERE id=?",
                (callback.from_user.id, datetime.utcnow().isoformat(), req_id),
            )
            await db.commit()
        await _log_action(
            tid,
            callback.from_user.id,
            "nickname_check_approve",
            "tournament_nickname_checks",
            req_id,
            {"user_id": user_id, "requested_nickname": nickname},
        )
        await _queue_sync(
            tid,
            "nickname_approved",
            {"text": f"Judge approved nickname for user {user_id}: {nickname}"},
        )
        await callback.answer("Approved")
        await callback.message.edit_text(f"✅ Nickname request #{req_id} approved")
        try:
            await bot.send_message(created_by, f"✅ Your nickname request #{req_id} was approved")
        except Exception:
            pass

    @dp.callback_query(F.data.startswith("xnickno|"))
    async def nickcheck_reject_start(callback: CallbackQuery, state: FSMContext) -> None:
        req_id = int((callback.data or "").split("|", 1)[1])
        row = await _fetchone("SELECT tournament_id, status FROM tournament_nickname_checks WHERE id=?", (req_id,))
        if not row:
            await callback.answer("Request not found", show_alert=True)
            return
        tid, status = row
        t = await _tournament_core(tid)
        if not t or not _is_judge(t, callback.from_user.id, ops):
            await callback.answer("Only judge can reject", show_alert=True)
            return
        if status != "open":
            await callback.answer("Already decided", show_alert=True)
            return
        await state.set_state(ExtraState.waiting_reject_reason)
        await state.update_data(reject_type="nick", reject_req_id=req_id, reject_tid=tid)
        await callback.answer()
        await callback.message.answer("Send reject reason in one message")

    @dp.message(ExtraState.waiting_reject_reason)
    async def reject_reason_submit(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        req_id = int(data["reject_req_id"])
        tid = int(data["reject_tid"])
        reason = (message.text or "").strip()[:500] or "no reason"
        if data["reject_type"] == "replacement":
            row = await _fetchone(
                "SELECT status, created_by FROM tournament_replacement_requests WHERE id=?",
                (req_id,),
            )
            if not row or row[0] != "open":
                await message.answer("Request already decided")
                await state.clear()
                return
            created_by = row[1]
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE tournament_replacement_requests SET status='rejected', judge_id=?, decided_at=?, reject_reason=? WHERE id=?",
                    (message.from_user.id, datetime.utcnow().isoformat(), reason, req_id),
                )
                await db.commit()
            await _log_action(
                tid,
                message.from_user.id,
                "replacement_request_reject",
                "tournament_replacement_requests",
                req_id,
                {"reason": reason},
            )
            try:
                await bot.send_message(created_by, f"❌ Your replacement request #{req_id} was rejected: {reason}")
            except Exception:
                pass
            await message.answer("Replacement request rejected")
        else:
            row = await _fetchone(
                "SELECT status, created_by FROM tournament_nickname_checks WHERE id=?",
                (req_id,),
            )
            if not row or row[0] != "open":
                await message.answer("Request already decided")
                await state.clear()
                return
            created_by = row[1]
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE tournament_nickname_checks SET status='rejected', judge_id=?, decided_at=?, reject_reason=? WHERE id=?",
                    (message.from_user.id, datetime.utcnow().isoformat(), reason, req_id),
                )
                await db.commit()
            await _log_action(
                tid,
                message.from_user.id,
                "nickname_check_reject",
                "tournament_nickname_checks",
                req_id,
                {"reason": reason},
            )
            try:
                await bot.send_message(created_by, f"❌ Your nickname request #{req_id} was rejected: {reason}")
            except Exception:
                pass
            await message.answer("Nickname request rejected")
        await state.clear()

    @dp.callback_query(F.data == "xmenu|judgepanel")
    async def judge_panel(callback: CallbackQuery) -> None:
        uid = callback.from_user.id
        lang = await _lang(uid)
        rep_rows = await _fetchall(
            """
            SELECT id, tournament_id, match_id, out_user_id, in_user_id, reason, created_by
            FROM tournament_replacement_requests
            WHERE status='open'
            ORDER BY id DESC
            LIMIT 20
            """
        )
        nick_rows = await _fetchall(
            """
            SELECT id, tournament_id, user_id, requested_nickname, created_by
            FROM tournament_nickname_checks
            WHERE status='open'
            ORDER BY id DESC
            LIMIT 20
            """
        )
        sent = 0
        for rid, tid, mid, out_uid, in_uid, reason, created_by in rep_rows:
            t = await _tournament_core(tid)
            if not t or not _is_judge(t, uid, ops):
                continue
            sent += 1
            await callback.message.answer(
                f"Replacement request #{rid}\nTournament #{tid}, match #{mid}\nout: {out_uid}\nin: {in_uid}\nby: {created_by}\nreason: {reason}",
                reply_markup=_replacement_review_kb(rid),
            )
        for rid, tid, user_id, nickname, created_by in nick_rows:
            t = await _tournament_core(tid)
            if not t or not _is_judge(t, uid, ops):
                continue
            sent += 1
            await callback.message.answer(
                f"Nickname check #{rid}\nTournament #{tid}\nuser: {user_id}\nnickname: {nickname}\nby: {created_by}",
                reply_markup=_nick_review_kb(rid),
            )
        if sent == 0:
            await callback.answer(_tr(lang, "No open judge requests", "Нет открытых заявок для судьи"), show_alert=True)
            return
        await callback.answer()

    @dp.message(ExtraState.waiting_deputy)
    async def set_deputy_submit(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        parts = [p.strip() for p in text.split(",", 1)]
        if len(parts) != 2:
            await message.answer("Wrong format. Need: tournament_id, deputy_user_id")
            return
        try:
            tid = int(parts[0])
            deputy_uid = int(parts[1])
        except Exception:
            await message.answer("tournament_id and deputy_user_id must be numbers")
            return
        t = await _tournament_core(tid)
        if not t:
            await message.answer("Tournament not found")
            return
        if not _has_scope(t, message.from_user.id, ops, "manage_tournament"):
            await message.answer("No access to set deputy")
            return
        scopes = ["manage_participants", "approve_replacements", "manage_judges"]
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE tournaments SET deputy_founder_id=?, deputy_scope_json=?, updated_at=? WHERE id=?",
                (deputy_uid, json.dumps(scopes, ensure_ascii=False), datetime.utcnow().isoformat(), tid),
            )
            await db.commit()
        await _log_action(
            tid,
            message.from_user.id,
            "set_deputy_founder",
            "tournaments",
            tid,
            {"deputy_founder_id": deputy_uid, "deputy_scope": scopes},
        )
        await _queue_sync(tid, "deputy_set", {"text": f"Tournament #{tid}: deputy founder set to {deputy_uid}"})
        await message.answer(f"✅ Deputy founder set: {deputy_uid}")
        try:
            await bot.send_message(deputy_uid, f"You were assigned as deputy founder for tournament #{tid}")
        except Exception:
            pass
        await state.clear()
