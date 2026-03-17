
import os
import asyncio
import random
import re
import socket
import requests
from pathlib import Path
from datetime import datetime, timedelta, UTC
from difflib import SequenceMatcher
from typing import Optional, Any, AsyncGenerator, cast
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.session.base import BaseSession
from aiogram.client.session.middlewares.request_logging import RequestLogging
from aiogram.methods import TelegramMethod
from aiogram.types import InputFile
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    BotCommand,
    MenuButtonWebApp,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramNetworkError
from dotenv import load_dotenv

if os.name == "nt":
    # Workaround for occasional TLS handshake stalls with aiohttp on Windows Proactor loop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from openai_vision_table import extract_player_names  # Names-only OCR parser

from database import (
    init_db,
    save_user_name,
    get_user_name,
    get_user_language,
    set_user_language,
    get_all_user_names,
    get_user_id_by_name,
    add_points,
    is_user_verified,
    create_verification_request,
    get_verification_request,
    set_verification_request_status,
    upsert_verified_account,
    # name change
    create_name_change_request,
    get_name_change_request,
    set_name_change_request_status,
    has_open_name_change_request,
    # operator lists
    list_open_verification_requests,
    list_open_name_change_requests,
    # offseason
    apply_offseason_result,
)

# =========================
# CONFIG
# =========================
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Add it to .env")

PLAYERS_PER_MATCH = 3  # set to 8 in production
CONFIRM_MINUTES = 15
SECOND_CONFIRM_MINUTES = 5
MIN_CONFIRMED_TO_START = 3
BAN_HOURS = 1

LEADERBOARD_URL = os.getenv("LEADERBOARD_URL", "https://your-site.example")
EXAMPLE_SCREENSHOT_FILE_ID = os.getenv("EXAMPLE_SCREENSHOT_FILE_ID")  # can be None

POINTS_BY_PLACE = [5, 4, 3, 2, 1, 0, -1, -2]  # top-8
OPERATORS = {5538733181}  # TG operator IDs

# =========================
# BOT
# =========================
class RequestsSession(BaseSession):
    def __init__(self, timeout: int = 30) -> None:
        super().__init__()
        self.timeout = timeout
        self.middleware(RequestLogging())

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        url = self.api.api_url(token=bot.token, method=method.__api_method__)
        files: dict[str, InputFile] = {}
        data: dict[str, Any] = {}

        values = method.model_dump(warnings=False)
        for key, value in values.items():
            prepared = self.prepare_value(value, bot=bot, files=files)
            if prepared is not None:
                data[key] = prepared

        if files:
            raise TelegramNetworkError(method=method, message="Local file upload is not supported in RequestsSession")

        try:
            resp = await asyncio.to_thread(
                requests.post,
                url,
                data=data,
                timeout=timeout or self.timeout,
            )
            raw = resp.text
        except requests.RequestException as e:
            raise TelegramNetworkError(method=method, message=f"Requests error: {e}") from e

        response = self.check_response(
            bot=bot,
            method=method,
            status_code=resp.status_code,
            content=raw,
        )
        return cast(Any, response.result)

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        try:
            resp = await asyncio.to_thread(
                requests.get,
                url,
                headers=headers,
                timeout=timeout,
                stream=True,
            )
            if raise_for_status:
                resp.raise_for_status()
        except requests.RequestException as e:
            raise TelegramNetworkError(method=None, message=f"Requests stream error: {e}") from e

        for chunk in resp.iter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk


bot_session = RequestsSession(timeout=30)
bot = Bot(token=TOKEN, session=bot_session)
dp = Dispatcher()

# =========================
# SAFE TELEGRAM OPS (no-crash on blocked users / bad edits)
# =========================
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

async def safe_send(chat_id: int, text: str, **kwargs):
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except TelegramForbiddenError:
        return None
    except TelegramBadRequest:
        return None
    except Exception:
        return None

async def safe_edit_text(chat_id: int, message_id: int, text: str, **kwargs):
    try:
        return await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, **kwargs)
    except TelegramForbiddenError:
        return None
    except TelegramBadRequest:
        return None
    except Exception:
        return None

async def safe_delete(chat_id: int, message_id: int):
    try:
        return await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramForbiddenError:
        return None
    except TelegramBadRequest:
        return None
    except Exception:
        return None


# =========================
# GLOBAL STATE (RAM)
# =========================
active_teams: dict[str, list[str]] = {}            # team_id -> [player_names]
team_timers: dict[str, dict[str, datetime]] = {}   # team_id -> {player_name: expires_utc}
banned_users: dict[int, datetime] = {}             # tg_user_id -> banned_until_utc
team_captains: dict[str, str] = {}                 # team_id -> captain_name
search_queue: set[int] = set()                     # tg_user_id set
match_lock = asyncio.Lock()                            # prevents double match creation
match_results_sent: set[str] = set()               # team_id already finalized
started_matches: set[str] = set()                    # team_id already started


# confirmation/queue status
team_confirmed: dict[str, set[str]] = {}           # team_id -> confirmed player_names
team_phase: dict[str, int] = {}                    # team_id -> 1 or 2 (second confirm)
team_confirm_messages: dict[str, dict[int, int]] = {}  # team_id -> {tg_user_id: message_id}
queue_status_messages: dict[int, int] = {}         # tg_user_id -> message_id

# per-team deadlines for confirmation windows
team_deadline: dict[str, datetime] = {}            # team_id -> window end (UTC)
# map team_id -> {player_name: tg_user_id} to avoid resolving ids by name
team_name_to_uid: dict[str, dict[str, int]] = {}

# confirmation rules
TARGET_CONFIRMATIONS = 8       # start immediately when reached
MIN_START_ON_TIMEOUT = 6       # if window expires, start with at least this many

# user platform (pc/android)
user_platform: dict[int, str] = {}            # tg_user_id -> 'pc'|'android'
waiting_platform: set[int] = set()            # who is choosing platform right now

# votes for "No code" -> transfer captain
team_no_code_votes: dict[str, set[int]] = {}    # team_id -> voter tg_user_ids


# =========================
# FSM
# =========================
class Form(StatesGroup):
    waiting_for_code = State()
    waiting_for_platform = State()
    waiting_for_language = State()
    change_name_wait_new = State()
    change_name_wait_photo = State()

class Verify(StatesGroup):
    wait_nick_uid = State()
    wait_profile = State()
    wait_chat = State()

# =========================
# TIME HELPERS
# =========================
def now_utc() -> datetime:
    return datetime.now(UTC)

# =========================
# I18N
# =========================
TEXTS = {
    "ru": {
        "requests": "Requests",
        "verification": "Verification",
        "my_name": "My name",
        "change_name": "Change name",
        "cancel_search": "Cancel search",
        "find_match": "Find match",
        "leaderboard": "Leaderboard",
        "settings": "Settings",
        "lang_ru": "RU \u0420\u0443\u0441\u0441\u043a\u0438\u0439",
        "lang_en": "EN English",
        "choose_lang": "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u044f\u0437\u044b\u043a:",
        "lang_saved_ru": "\u2705 \u042f\u0437\u044b\u043a \u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447\u0435\u043d: \u0420\u0443\u0441\u0441\u043a\u0438\u0439",
        "lang_saved_en": "\u2705 \u042f\u0437\u044b\u043a \u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447\u0435\u043d: English",
        "start_need_lang": "\u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u0432\u044b\u0431\u0435\u0440\u0438 \u044f\u0437\u044b\u043a.",
        "start_need_verify": "\u0414\u043e\u0431\u0440\u043e \u043f\u043e\u0436\u0430\u043b\u043e\u0432\u0430\u0442\u044c! \u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u043f\u0440\u043e\u0439\u0434\u0438 \u0432\u0435\u0440\u0438\u0444\u0438\u043a\u0430\u0446\u0438\u044e.\n\u041d\u0430\u0436\u043c\u0438 Verification.",
        "start_welcome_back": "\u0421 \u0432\u043e\u0437\u0432\u0440\u0430\u0449\u0435\u043d\u0438\u0435\u043c, {name}!",
        "start_not_verified": "\u041f\u0440\u0438\u0432\u0435\u0442, {name}!\n\u0422\u044b \u0435\u0449\u0435 \u043d\u0435 \u0432\u0435\u0440\u0438\u0444\u0438\u0446\u0438\u0440\u043e\u0432\u0430\u043d.\n\u041d\u0430\u0436\u043c\u0438 Verification.",
        "settings_text": "\u041e\u0442\u043a\u0440\u043e\u0439 \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438 \u044f\u0437\u044b\u043a\u0430:",
    },
    "en": {
        "requests": "Requests",
        "verification": "Verification",
        "my_name": "My name",
        "change_name": "Change name",
        "cancel_search": "Cancel search",
        "find_match": "Find match",
        "leaderboard": "Leaderboard",
        "settings": "Settings",
        "lang_ru": "RU Русский",
        "lang_en": "EN English",
        "choose_lang": "Choose language:",
        "lang_saved_ru": "\u2705 Language switched: Russian",
        "lang_saved_en": "\u2705 Language switched: English",
        "start_need_lang": "Choose a language first.",
        "start_need_verify": "Welcome! First you need to pass verification.\nPress Verification.",
        "start_welcome_back": "Welcome back, {name}!",
        "start_not_verified": "Hello, {name}!\nYou are not verified yet.\nPress Verification.",
        "settings_text": "Open language settings:",
    },
}


def _text(lang: str, key: str, **kwargs) -> str:
    value = TEXTS.get(lang, TEXTS["ru"]).get(key, TEXTS["en"].get(key, key))
    return value.format(**kwargs) if kwargs else value


def tr(lang: str, en: str, ru: str) -> str:
    return en if lang == "en" else ru


async def get_lang(user_id: int) -> str:
    lang = await get_user_language(user_id)
    return lang if lang in ("ru", "en") else "ru"


def button_variants(key: str) -> set[str]:
    return {_text("ru", key), _text("en", key)}


def button_is(text: str | None, key: str) -> bool:
    return (text or "").strip() in button_variants(key)


def parse_lang_from_text(text: str | None) -> str | None:
    value = (text or "").strip()
    if value == _text("ru", "lang_ru"):
        return "ru"
    if value == _text("ru", "lang_en"):
        return "en"
    return None


def kb_language_select() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=_text("ru", "lang_ru")), KeyboardButton(text=_text("ru", "lang_en"))],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )

# =========================
# KEYBOARDS (dynamic)
# =========================
def add_operator_row_lang(kb: ReplyKeyboardMarkup, lang: str) -> ReplyKeyboardMarkup:
    kb.keyboard.append([KeyboardButton(text=_text(lang, "requests"))])
    return kb


def kb_only_verification(lang: str, is_operator: bool = False) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=_text(lang, "verification"))],
            [KeyboardButton(text=_text(lang, "settings"))],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )
    return add_operator_row_lang(kb, lang) if is_operator else kb


def kb_not_verified(lang: str, in_queue: bool = False, is_operator: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=_text(lang, "verification"))],
        [KeyboardButton(text=_text(lang, "my_name")), KeyboardButton(text=_text(lang, "change_name"))],
        [KeyboardButton(text=_text(lang, "settings"))],
    ]
    if in_queue:
        rows.append([KeyboardButton(text=_text(lang, "cancel_search"))])
    kb = ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, one_time_keyboard=False)
    return add_operator_row_lang(kb, lang) if is_operator else kb


def kb_verified(lang: str, in_queue: bool = False, is_operator: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=_text(lang, "find_match")), KeyboardButton(text=_text(lang, "my_name"))],
        [KeyboardButton(text=_text(lang, "change_name"))],
        [KeyboardButton(text=_text(lang, "settings"))],
    ]
    if in_queue:
        rows.append([KeyboardButton(text=_text(lang, "cancel_search"))])

    rows.append([KeyboardButton(text=_text(lang, "leaderboard"), web_app=WebAppInfo(url=LEADERBOARD_URL))])
    kb = ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, one_time_keyboard=False)
    return add_operator_row_lang(kb, lang) if is_operator else kb


async def get_keyboard_for_user(user_id: int) -> ReplyKeyboardMarkup:
    is_operator = user_id in OPERATORS
    lang = await get_lang(user_id)
    name = await get_user_name(user_id)

    if not name:
        return kb_only_verification(lang, is_operator=is_operator)

    verified = await is_user_verified(user_id)
    if verified:
        return kb_verified(lang, in_queue=(user_id in search_queue), is_operator=is_operator)

    return kb_not_verified(lang, in_queue=(user_id in search_queue), is_operator=is_operator)


# =========================
# PLATFORM CHOICE# =========================
def platform_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="рџ–Ґ PC", callback_data="platform|pc"),
                InlineKeyboardButton(text="рџ“± Android", callback_data="platform|android"),
            ]
        ]
    )

# =========================
# LIVE COUNTERS (queue + confirmation)
# =========================
def build_team_status_text(team_id: str, window_minutes: int) -> str:
    players = active_teams.get(team_id, [])
    confirmed = team_confirmed.get(team_id, set())
    pending = team_timers.get(team_id, {})
    phase = team_phase.get(team_id, 1)

    confirmed_now = [p for p in players if p in confirmed]
    confirmed_count = len(confirmed_now)
    total = len(players)
    queue_count = len(search_queue)

    header = (
        f"рџЋ® Match found!\n"
        f"Phase: {phase}/2 | Confirm window: {window_minutes} min\n"
        f"вњ… Confirmed: {confirmed_count}/{total} (min {MIN_CONFIRMED_TO_START})\n"
        f"рџ”Ќ In search: {queue_count}\n\n"
    )

    lines = []
    for i, p in enumerate(players, 1):
        if p in confirmed:
            mark = "вњ…"
        elif p in pending:
            mark = "вЏі"
        else:
            mark = "вќЊ"
        lines.append(f"{i}. {p} {mark}")

    return header + "\n".join(lines)

async def update_team_confirm_messages(team_id: str, window_minutes: int):
    msg_map = team_confirm_messages.get(team_id, {})
    if not msg_map:
        return

    text = build_team_status_text(team_id, window_minutes)

    for uid, mid in list(msg_map.items()):
        # Determine which player this uid represents (by saved name)
        try:
            name = await get_user_name(uid)
        except Exception:
            name = None

        # If user is not in this team anymore вЂ” try to clean mapping
        if not name or name not in active_teams.get(team_id, []):
            msg_map.pop(uid, None)
            continue

        # If already confirmed or removed from timers => no button
        if name in team_confirmed.get(team_id, set()) or name not in team_timers.get(team_id, {}):
            markup = None
        else:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="вњ… Confirm participation", callback_data=f"confirm|{team_id}|{name}")]]
            )

        try:
            await bot.edit_message_text(text=text + f"\n\nPlease confirm within {window_minutes} minutes.",
                                        chat_id=uid,
                                        message_id=mid,
                                        reply_markup=markup)
        except Exception:
            # message might be uneditable (old, deleted, etc.)
            pass

async def _collect_queue_candidates() -> list[tuple[int, str]]:
    """Return eligible (uid, name) currently in queue and clean invalid entries."""
    candidates: list[tuple[int, str]] = []
    for uid in list(search_queue):
        try:
            if is_banned_sync(uid) or await user_in_any_team(uid):
                search_queue.discard(uid)
                continue
            if not await is_user_verified(uid):
                search_queue.discard(uid)
                continue
            nm = await get_user_name(uid)
            if not nm:
                search_queue.discard(uid)
                continue
            candidates.append((uid, nm))
        except Exception:
            # If anything goes wrong for this uid, drop it to keep queue healthy
            search_queue.discard(uid)
    return candidates


def _build_queue_text(eligible_count: int) -> str:
    need_more = max(0, PLAYERS_PER_MATCH - eligible_count)
    return (
        "рџ”Ќ You are in the search queue.\n"
        f"Players in search: {eligible_count}/{PLAYERS_PER_MATCH}\n"
        f"Need {need_more} more to start a match."
    )


async def send_or_update_queue_status(user_id: int, text: str | None = None):
    """Maintain ONE live status message per user while they are in the search queue."""
    if user_id not in search_queue:
        mid = queue_status_messages.pop(user_id, None)
        if mid:
            try:
                await safe_delete(user_id, mid)
            except Exception:
                pass
        return

    if text is None:
        candidates = await _collect_queue_candidates()
        text = _build_queue_text(len(candidates))

    mid = queue_status_messages.get(user_id)

    if mid:
        ok = await safe_edit_text(chat_id=user_id, message_id=mid, text=text)
        if ok is not None:
            return
        queue_status_messages.pop(user_id, None)

    msg = await safe_send(user_id, text, reply_markup=await get_keyboard_for_user(user_id))
    if msg:
        queue_status_messages[user_id] = msg.message_id


async def update_queue_status_for_all():
    candidates = await _collect_queue_candidates()
    text = _build_queue_text(len(candidates))

    # Update all currently queued users
    for uid in list(search_queue):
        await send_or_update_queue_status(uid, text=text)

    # Also delete stale status messages for users who already left the queue
    for uid in list(queue_status_messages.keys()):
        if uid not in search_queue:
            await send_or_update_queue_status(uid, text=text)


async def _start_match_from_selected(selected: list[tuple[int, str]]):
    """Create a match, send confirm messages, pick captain (prefer PC)."""
    team_user_ids = [uid for uid, _ in selected]
    selected_players = [nm for _, nm in selected]

    # Remove from queue and clean their live queue messages
    for uid in team_user_ids:
        search_queue.discard(uid)
        await send_or_update_queue_status(uid)
    await update_queue_status_for_all()

    team_id = f"team-{team_user_ids[0]}-{int(asyncio.get_event_loop().time()*1000)}"

    # choose captain preferably on PC
    pc_candidates = [(uid, nm) for uid, nm in selected if user_platform.get(uid) == "pc"]
    if pc_candidates:
        captain_uid, captain_name = random.choice(pc_candidates)
    else:
        captain_uid, captain_name = random.choice(selected)

    team_captains[team_id] = captain_name

    active_teams[team_id] = selected_players
    team_timers[team_id] = {}
    team_confirmed[team_id] = set()
    team_phase[team_id] = 1
    team_confirm_messages[team_id] = {}
    team_name_to_uid[team_id] = {nm: uid for uid, nm in selected}
    # set confirmation window deadline and per-player timers
    deadline = now_utc() + timedelta(minutes=CONFIRM_MINUTES)
    team_deadline[team_id] = deadline
    for nm in selected_players:
        team_timers[team_id][nm] = deadline

    team_message = build_team_status_text(team_id, CONFIRM_MINUTES)

    # Send confirm messages using the already-known Telegram user_ids.
    # IMPORTANT: Do NOT re-resolve by name (names may be non-unique / not searchable),
    # otherwise the match can "never start" because nobody receives the confirm message.
    for uid, p in selected:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="вњ… Confirm participation", callback_data=f"confirm|{team_id}|{p}")]]
        )
        msg = await safe_send(uid, team_message + f"\n\nPlease confirm within {CONFIRM_MINUTES} minutes.", reply_markup=kb)
        if msg:
            team_confirm_messages.setdefault(team_id, {})[uid] = msg.message_id
        # Start/track the confirm timer even if sending fails (blocked users will be handled by timeouts)
        team_timers[team_id][p] = now_utc() + timedelta(minutes=CONFIRM_MINUTES)

    # Captain panel to captain_uid (single message, no extra spam)
    await safe_send(
        captain_uid,
        team_message + "\nрџ”‘ You are the captain!\n\nрџ“ё When match ends, press рџЏЃ Open results so everyone can send screenshots.",
        reply_markup=captain_panel_keyboard(team_id),
    )


async def try_make_matches_from_queue():
    """Try to create as many matches as possible from current queue."""
    async with match_lock:
        candidates = await _collect_queue_candidates()
        # Keep a stable ordering (join order isn't tracked, so use uid order for determinism)
        candidates.sort(key=lambda x: x[0])

        while len(candidates) >= PLAYERS_PER_MATCH:
            selected = candidates[:PLAYERS_PER_MATCH]
            candidates = candidates[PLAYERS_PER_MATCH:]
            await _start_match_from_selected(selected)

        # refresh live queue counters after possible pops
        await update_queue_status_for_all()
async def kb_name_edit(user_id: int) -> ReplyKeyboardMarkup:
    base = await get_keyboard_for_user(user_id)
    lang = await get_lang(user_id)
    cancel_text = "❌ Cancel name edit" if lang == "en" else "❌ Отменить смену имени"
    # prepend cancel button
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=cancel_text)]] + (base.keyboard or []),
        resize_keyboard=True,
        one_time_keyboard=False,
    )



# =========================
# INLINE KEYBOARDS
# =========================
def captain_panel_keyboard(team_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Send code to team", callback_data=f"send_code_{team_id}")],
            [InlineKeyboardButton(text="рџЋ– Transfer captain", callback_data=f"change_captain|{team_id}")],
        ]
    )

def requests_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="вњ… Verification requests", callback_data="op_req|ver")
    kb.button(text="вњЏпёЏ Name change requests", callback_data="op_req|name")
    kb.adjust(1, 1)
    return kb.as_markup()

def requests_list_kb(prefix: str, ids: list[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for rid in ids[:12]:
        kb.button(text=f"Open #{rid}", callback_data=f"op_open|{prefix}|{rid}")
    kb.button(text="в¬…пёЏ Back", callback_data="op_req|menu")
    kb.adjust(2, 2, 2, 2, 2, 1)
    return kb.as_markup()

# =========================
# VERIFICATION HELPERS
# =========================
def generate_code_word() -> str:
    return f"VERIFY-{random.randint(1000, 9999)}"

def operator_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="вњ… Approve", callback_data=f"vr_ok|{req_id}")],
            [InlineKeyboardButton(text="вќЊ Reject", callback_data=f"vr_no|{req_id}")],
        ]
    )

def parse_nick_uid(text: str) -> Optional[tuple[str, str]]:
    parts = (text or "").strip().split()
    if len(parts) < 2:
        return None
    uid = parts[-1].strip()
    nick = " ".join(parts[:-1]).strip()

    if not (2 <= len(nick) <= 32):
        return None
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9 _\-\|]{2,32}", nick):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_\-]{3,32}", uid):
        return None

    return nick, uid

# =========================
# CHANGE NAME VIA OPERATOR
# =========================
def name_change_op_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="вњ… Approve name", callback_data=f"cn_ok|{req_id}")],
            [InlineKeyboardButton(text="вќЊ Reject name", callback_data=f"cn_no|{req_id}")],
        ]
    )

# =========================
# MATCHING HELPERS (fuzzy)
# =========================
HOMOGLYPHS = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t", "у": "y", "х": "x",
    "І": "I", "і": "i", "Ё": "E", "ё": "e",
})
LEVEL_SPLIT_RE = re.compile(r"(?i)\b(уров\w*|ypob\w*|ур0в\w*|level\w*)\b")

def normalize_name(s: str) -> str:
    s = (s or "").strip()
    s = s.replace("丨", "|").replace("l", "|").replace("i", "|")
    s = s.translate(HOMOGLYPHS)
    s = s.lower()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[^a-z0-9\|_а-я]", "", s)
    return s

def extract_core_nick(ocr_name: str) -> str:
    if not ocr_name:
        return ""
    s = (ocr_name or "").strip()
    s = LEVEL_SPLIT_RE.split(s)[0].strip()
    tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9_\|]{2,}", s)
    if not tokens:
        return ""
    return max(tokens, key=len)

async def find_user_id_fuzzy(ocr_name: str, team_players: list[str]) -> tuple[int | None, str | None, float]:
    core = extract_core_nick(ocr_name)
    if not core:
        return None, None, 0.0

    q = normalize_name(core)

    def ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()

    # 1) within team
    best_score = 0.0
    best_name = None
    for p in team_players:
        sc = ratio(q, normalize_name(p))
        if sc > best_score:
            best_score = sc
            best_name = p
    if best_name and best_score >= 0.60:
        uid = await get_user_id_by_name(best_name)
        return uid, best_name, best_score

    # 2) whole DB
    db_names = await get_all_user_names()
    best_score = 0.0
    best_name = None
    for dbn in db_names:
        sc = ratio(q, normalize_name(dbn))
        if sc > best_score:
            best_score = sc
            best_name = dbn
    if best_name and best_score >= 0.65:
        uid = await get_user_id_by_name(best_name)
        return uid, best_name, best_score

    return None, None, best_score

# =========================
# BAN / TEAM HELPERS
# =========================
def is_banned_sync(user_id: int) -> bool:
    until = banned_users.get(user_id)
    if not until:
        return False
    if now_utc() < until:
        return True
    banned_users.pop(user_id, None)
    return False

def get_ban_remaining(user_id: int):
    until = banned_users.get(user_id)
    if until and now_utc() < until:
        return until - now_utc()
    return None

async def user_in_any_team(user_id: int) -> bool:
    name = await get_user_name(user_id)
    if not name:
        return False
    return any(name in players for players in active_teams.values())

async def find_team_id_by_user_id(user_id: int) -> str | None:
    name = await get_user_name(user_id)
    if not name:
        return None
    for tid, players in active_teams.items():
        if name in players:
            return tid
    return None

async def captain_id_for_team(team_id: str) -> int | None:
    cap_name = team_captains.get(team_id)
    if not cap_name:
        return None
    return team_name_to_uid.get(team_id, {}).get(cap_name)

# =========================
# STARTUP + TIMERS
# =========================
@dp.startup()
async def on_startup():
    await init_db()
    try:
        parsed = urlparse(LEADERBOARD_URL)
        if parsed.scheme == "https" and parsed.hostname:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Leaderboard",
                    web_app=WebAppInfo(url=LEADERBOARD_URL),
                )
            )
            print("[startup] set_chat_menu_button: ok", flush=True)
        else:
            print(f"[startup] set_chat_menu_button skipped: invalid LEADERBOARD_URL={LEADERBOARD_URL!r}", flush=True)
    except Exception as e:
        print(f"[startup] set_chat_menu_button skipped: {e}", flush=True)

    try:
        host = urlparse(LEADERBOARD_URL).hostname
        if host:
            socket.gethostbyname(host)
    except Exception:
        print(f"[startup] LEADERBOARD_URL host is not resolvable: {LEADERBOARD_URL}", flush=True)

    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Open main menu"),
                BotCommand(command="help", description="Help"),
                BotCommand(command="leaderboard", description="Open leaderboard"),
                BotCommand(command="settings", description="Settings"),
                BotCommand(command="language", description="Change language"),
            ]
        )
        print("[startup] set_my_commands: ok", flush=True)
    except Exception as e:
        print(f"[startup] set_my_commands skipped: {e}", flush=True)
    asyncio.create_task(check_timers())
    print("Database initialized.", flush=True)

async def check_timers():
    while True:
        await asyncio.sleep(5)
        now = now_utc()

        for team_id, timers in list(team_timers.items()):
            for player_name, expires in list(timers.items()):
                if now >= expires:
                    timers.pop(player_name, None)

                    if team_id in active_teams and player_name in active_teams[team_id]:
                        active_teams[team_id].remove(player_name)

                    try:
                        uid = team_name_to_uid.get(team_id, {}).get(player_name)
                        if uid:
                            banned_users[uid] = now + timedelta(hours=BAN_HOURS)
                            await safe_send(
                                uid,
                                "вЏ° You did not confirm participation in time.\n"
                                f"You are banned from matches for {BAN_HOURS} hour(s).",
                                reply_markup=await get_keyboard_for_user(uid),
                            )
                    except Exception as e:
                        print(f"[TIMER] Failed to ban/notify {player_name}: {e}")

            # refresh live counters
            window = CONFIRM_MINUTES if team_phase.get(team_id, 1) == 1 else SECOND_CONFIRM_MINUTES
            await update_team_confirm_messages(team_id, window)

            # end of confirmation window is driven by team_deadline (not by all timers being popped)
            deadline = team_deadline.get(team_id)
            if deadline and now >= deadline:
                # prevent double-run
                team_deadline.pop(team_id, None)
                await on_confirmation_window_end(team_id)
async def start_match(team_id: str):
    players = active_teams.get(team_id, [])
    if not players:
        return

    # stop any pending confirmation deadline
    team_deadline.pop(team_id, None)

    text = "рџљЂ The match is starting!\n\nPlayers:\n"
    for i, name in enumerate(players, 1):
        text += f"{i}. {name}\n"

    for name in players:
        uid = team_name_to_uid.get(team_id, {}).get(name)
        if uid:
            await safe_send(uid, text, reply_markup=await get_keyboard_for_user(uid))

    team_timers.pop(team_id, None)
    # refresh confirmation messages (remove buttons)
    await update_team_confirm_messages(team_id, CONFIRM_MINUTES if team_phase.get(team_id, 1) == 1 else SECOND_CONFIRM_MINUTES)

    cap_id = await captain_id_for_team(team_id)
    if cap_id:
        try:
            await bot.send_message(
                cap_id,
                "рџ“ё Send the final results screenshot here.",
                reply_markup=await get_keyboard_for_user(cap_id),
            )
        except Exception:
            pass

        if EXAMPLE_SCREENSHOT_FILE_ID:
            try:
                await bot.send_photo(cap_id, photo=EXAMPLE_SCREENSHOT_FILE_ID, caption="Example screenshot рџ‘†")
            except Exception:
                pass


# =========================
# CONFIRMATION WINDOWS: start / fill / cancel
# =========================
async def cleanup_match(team_id: str):
    team_timers.pop(team_id, None)
    # refresh confirmation messages (remove buttons)
    await update_team_confirm_messages(team_id, CONFIRM_MINUTES if team_phase.get(team_id, 1) == 1 else SECOND_CONFIRM_MINUTES)
    team_confirmed.pop(team_id, None)
    team_phase.pop(team_id, None)
    team_confirm_messages.pop(team_id, None)
    team_captains.pop(team_id, None)
    active_teams.pop(team_id, None)
    started_matches.discard(team_id)

async def cancel_match(team_id: str, reason: str = "Not enough confirmed players"):
    players = active_teams.get(team_id, [])
    text = f"вќЊ Match cancelled. Reason: {reason}"
    for name in players:
        uid = await get_user_id_by_name(name)
        if uid:
            try:
                await bot.send_message(uid, text, reply_markup=await get_keyboard_for_user(uid))
            except Exception:
                pass
    await cleanup_match(team_id)

async def fill_team_and_request_second_confirmation(team_id: str):
    # remove unconfirmed leftovers already removed by timer; we only have confirmed + maybe some still present
    team_phase[team_id] = 2

    players = active_teams.get(team_id, [])
    confirmed = team_confirmed.get(team_id, set())
    # keep confirmed players only (others should already be removed)
    players = [p for p in players if p in confirmed]
    active_teams[team_id] = players

    need = max(0, PLAYERS_PER_MATCH - len(players))
    if need == 0:
        # already full with confirmed players
        started_matches.add(team_id)
        team_deadline.pop(team_id, None)
        await start_match(team_id)
        return

    added: list[str] = []

    # 1) from queue first
    for uid in list(search_queue):
        if len(added) >= need:
            break
        if is_banned_sync(uid) or await user_in_any_team(uid):
            search_queue.discard(uid)
            continue
        if not await is_user_verified(uid):
            search_queue.discard(uid)
            continue
        nm = await get_user_name(uid)
        if not nm or nm in players or nm in added:
            search_queue.discard(uid)
            continue
        added.append(nm)
        team_name_to_uid.setdefault(team_id, {})[nm] = uid
        search_queue.discard(uid)
        await send_or_update_queue_status(uid)  # remove status message

    # 2) if still need, take any verified user not in match and not banned
    if len(added) < need:
        all_names = await get_all_user_names()
        random.shuffle(all_names)
        for nm in all_names:
            if len(added) >= need:
                break
            uid = await get_user_id_by_name(nm)
            if not uid:
                continue
            if is_banned_sync(uid) or await user_in_any_team(uid):
                continue
            if not await is_user_verified(uid):
                continue
            if nm in players or nm in added:
                continue
            added.append(nm)
            team_name_to_uid.setdefault(team_id, {})[nm] = uid

    if not added:
        await cancel_match(team_id, reason="No available players to fill the lobby")
        return

    # add them and start second confirm timers (only for new players)
    active_teams[team_id].extend(added)
    team_timers[team_id] = {}
    deadline2 = now_utc() + timedelta(minutes=SECOND_CONFIRM_MINUTES)
    team_deadline[team_id] = deadline2
    for nm in added:
        team_timers[team_id][nm] = deadline2

    # notify existing confirmed players about fill
    for nm in players:
        uid = team_name_to_uid.get(team_id, {}).get(nm)
        if uid:
            await safe_send(uid, "рџ§© Not enough confirmations. Filling players... Second confirm window: 5 minutes.")

    # send confirm messages to new players (and refresh for old ones)
    for nm in added:
        uid = team_name_to_uid.get(team_id, {}).get(nm)
        if not uid:
            continue
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="вњ… Confirm participation", callback_data=f"confirm|{team_id}|{nm}")]]
        )
        msg = await safe_send(uid, build_team_status_text(team_id, SECOND_CONFIRM_MINUTES) + f"\n\nPlease confirm within {SECOND_CONFIRM_MINUTES} minutes.", reply_markup=kb)
        if msg:
            team_confirm_messages.setdefault(team_id, {})[uid] = msg.message_id

    await update_team_confirm_messages(team_id, SECOND_CONFIRM_MINUTES)

async def on_confirmation_window_end(team_id: str):
    if team_id in started_matches:
        return
    if team_id not in active_teams:
        return

    confirmed = team_confirmed.get(team_id, set())
    players = active_teams.get(team_id, [])
    confirmed_count = len([p for p in players if p in confirmed])

    phase = team_phase.get(team_id, 1)

    if confirmed_count >= MIN_START_ON_TIMEOUT:
        started_matches.add(team_id)
        await start_match(team_id)
        return

    if phase == 1:
        await fill_team_and_request_second_confirmation(team_id)
        return

    # phase 2 and still not enough
    await cancel_match(team_id, reason=f"Only {confirmed_count} confirmed (need {MIN_START_ON_TIMEOUT})")
# =========================
# FINALIZE (NO DRAFT)
# =========================
async def finalize_results_direct(team_id: str, names: list[str]):
    if team_id in match_results_sent:
        return

    names = (names or [])[:8]
    team_players = active_teams.get(team_id, [])

    medals = ["рџҐ‡", "рџҐ€", "рџҐ‰"]
    lines = ["рџЏ† Results:"]

    for place, name in enumerate(names, start=1):
        pts = POINTS_BY_PLACE[place - 1] if place <= len(POINTS_BY_PLACE) else 0

        uid, matched_name, sc = await find_user_id_fuzzy(name, team_players)
        if uid and matched_name:
            await add_points(uid, pts)

            old_slrpt, slrpt_delta, new_mult = await apply_offseason_result(uid, place)
            new_slrpt = old_slrpt + slrpt_delta

            prefix = medals[place - 1] if place <= 3 else f"{place}."
            extra = f" | SLRPT {slrpt_delta:+} (now {new_slrpt})"
            if place == 1:
                extra += f" | win x{new_mult:.2f}"
            elif place == 5:
                extra += " | win reset"

            lines.append(f"{prefix} {name} в†’ {matched_name} | {pts:+} pts (match {sc:.2f}){extra}")
        else:
            prefix = medals[place - 1] if place <= 3 else f"{place}."
            lines.append(f"{prefix} {name} | вќЊ not found in DB | {pts:+} pts (skipped)")

    lines.append("\nв„№ If someone is 'not found in DB' вЂ” their verified name in bot must match in-game nick.")
    result_text = "\n".join(lines)

    match_results_sent.add(team_id)

    for player_name in team_players:
        puid = await get_user_id_by_name(player_name)
        if puid:
            try:
                await bot.send_message(puid, result_text, reply_markup=await get_keyboard_for_user(puid))
            except Exception:
                pass

    # match finished -> cleanup
    await cleanup_match(team_id)

# =========================
# /start
# =========================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    lang = await get_lang(user_id)
    selected_lang = await get_user_language(user_id)

    if not selected_lang:
        await state.set_state(Form.waiting_for_language)
        await message.answer(
            f"{_text('ru', 'choose_lang')}\n{_text('en', 'choose_lang')}",
            reply_markup=kb_language_select(),
        )
        return

    kb = await get_keyboard_for_user(user_id)
    name = await get_user_name(user_id)
    if not name:
        await message.answer(_text(lang, "start_need_verify"), reply_markup=kb)
        return

    if await is_user_verified(user_id):
        await message.answer(_text(lang, "start_welcome_back", name=name), reply_markup=kb)
    else:
        await message.answer(_text(lang, "start_not_verified", name=name), reply_markup=kb)


@dp.message(Command("settings"))
@dp.message(Command("language"))
async def open_settings_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    lang = await get_lang(message.from_user.id)
    await message.answer(_text(lang, "settings_text"), reply_markup=kb_language_select())
    await state.set_state(Form.waiting_for_language)


@dp.message(lambda m: button_is(m.text, "settings"))
async def open_settings_btn(message: types.Message, state: FSMContext):
    await state.clear()
    lang = await get_lang(message.from_user.id)
    await message.answer(_text(lang, "settings_text"), reply_markup=kb_language_select())
    await state.set_state(Form.waiting_for_language)


@dp.message(lambda m: parse_lang_from_text(m.text) is not None)
async def select_language(message: types.Message, state: FSMContext):
    lang = parse_lang_from_text(message.text) or "ru"
    user_id = message.from_user.id
    await set_user_language(user_id, lang)
    await state.clear()

    kb = await get_keyboard_for_user(user_id)
    confirmation_key = "lang_saved_ru" if lang == "ru" else "lang_saved_en"
    await message.answer(_text(lang, confirmation_key), reply_markup=kb)

    name = await get_user_name(user_id)
    if not name:
        await message.answer(_text(lang, "start_need_verify"), reply_markup=kb)

# =========================
# OPERATOR BUTTON: рџ“‹ Requests
# =========================
@dp.message(lambda m: button_is(m.text, "requests"))
async def op_requests_btn(message: types.Message):
    if message.from_user.id not in OPERATORS:
        return
    lang = await get_lang(message.from_user.id)
    await message.answer(tr(lang, "Choose request type:", "Выбери тип заявки:"), reply_markup=requests_menu_kb())

@dp.callback_query(F.data.startswith("op_req|"))
async def op_req_menu(callback: types.CallbackQuery):
    lang = await get_lang(callback.from_user.id)
    if callback.from_user.id not in OPERATORS:
        await callback.answer(tr(lang, "No access.", "Нет доступа."))
        return

    _, which = (callback.data or "").split("|", 1)

    if which == "menu":
        await callback.message.edit_text(tr(lang, "Choose request type:", "Выбери тип заявки:"), reply_markup=requests_menu_kb())
        await callback.answer()
        return

    if which == "ver":
        rows = await list_open_verification_requests(limit=30)
        if not rows:
            await callback.message.edit_text(tr(lang, "No open verification requests.", "Нет открытых заявок на верификацию."), reply_markup=requests_menu_kb())
            await callback.answer()
            return

        ids = [r[0] for r in rows]
        text_lines = [tr(lang, "✅ Open verification requests:", "✅ Открытые заявки на верификацию:")]
        for (rid, user_id, tg_username, game_name, game_uid, created_at) in rows:
            uname = f"@{tg_username}" if tg_username else "вЂ”"
            text_lines.append(f"#{rid} | {uname} | {game_name} | {game_uid} | {created_at}")
        await callback.message.edit_text("\n".join(text_lines), reply_markup=requests_list_kb("ver", ids))
        await callback.answer()
        return

    if which == "name":
        rows = await list_open_name_change_requests(limit=30)
        if not rows:
            await callback.message.edit_text(tr(lang, "No open name change requests.", "Нет открытых заявок на смену имени."), reply_markup=requests_menu_kb())
            await callback.answer()
            return

        ids = [r[0] for r in rows]
        text_lines = [tr(lang, "✏️ Open name change requests:", "✏️ Открытые заявки на смену имени:")]
        for (rid, user_id, old_name, new_name, created_at) in rows:
            text_lines.append(f"#{rid} | {old_name or 'вЂ”'} в†’ {new_name} | {created_at}")
        await callback.message.edit_text("\n".join(text_lines), reply_markup=requests_list_kb("name", ids))
        await callback.answer()
        return

    await callback.answer(tr(lang, "Unknown.", "Неизвестно."))

@dp.callback_query(F.data.startswith("op_open|"))
async def op_open_request(callback: types.CallbackQuery):
    lang = await get_lang(callback.from_user.id)
    if callback.from_user.id not in OPERATORS:
        await callback.answer(tr(lang, "No access.", "Нет доступа."))
        return

    parts = (callback.data or "").split("|", 2)
    if len(parts) != 3:
        await callback.answer(tr(lang, "Bad data.", "Неверные данные."))
        return

    _, prefix, rid_s = parts
    rid = int(rid_s)

    if prefix == "ver":
        row = await get_verification_request(rid)
        if not row:
            await callback.answer(tr(lang, "Not found.", "Не найдено."))
            return

        (
            _id, user_id, tg_username, status,
            game_name, game_uid, code_word,
            profile_file_id, chat_file_id,
            created_at, decided_at, operator_id, reject_reason
        ) = row

        uname = f"@{tg_username}" if tg_username else "вЂ”"
        text = (
            f"вњ… Verification request #{rid}\n"
            f"tg_id: {user_id}\n"
            f"tg: {uname}\n"
            f"name: {game_name}\n"
            f"uid: {game_uid}\n"
            f"code: {code_word}\n"
            f"status: {status}\n"
            f"created: {created_at}"
        )

        await callback.message.answer(text, reply_markup=operator_kb(rid))
        await bot.send_photo(callback.from_user.id, profile_file_id, caption="Screenshot #1 (profile)")
        await bot.send_photo(callback.from_user.id, chat_file_id, caption="Screenshot #2 (chat)")
        await callback.answer(tr(lang, "Opened", "Открыто"))
        return

    if prefix == "name":
        row = await get_name_change_request(rid)
        if not row:
            await callback.answer(tr(lang, "Not found.", "Не найдено."))
            return

        _id, user_id, old_name, new_name, screenshot_file_id, status, created_at, decided_at, operator_id, reject_reason = row

        text = (
            f"вњЏпёЏ Name change request #{rid}\n"
            f"tg_id: {user_id}\n"
            f"old: {old_name or 'вЂ”'}\n"
            f"new: {new_name}\n"
            f"status: {status}\n"
            f"created: {created_at}"
        )
        await callback.message.answer(text, reply_markup=name_change_op_kb(rid))
        await bot.send_photo(callback.from_user.id, screenshot_file_id, caption="Name change proof screenshot")
        await callback.answer(tr(lang, "Opened", "Открыто"))
        return

    await callback.answer(tr(lang, "Unknown type.", "Неизвестный тип."))

# =========================
# MY NAME
# =========================
@dp.message(lambda m: button_is(m.text, "my_name"))
async def show_name_btn(message: types.Message):
    lang = await get_lang(message.from_user.id)
    name = await get_user_name(message.from_user.id)
    if name:
        text = f"Your current name: {name}" if lang == "en" else f"Твой текущий ник: {name}"
        await message.answer(text, reply_markup=await get_keyboard_for_user(message.from_user.id))
    else:
        text = (
            "You don't have a saved name yet. Press ✅ Verification first."
            if lang == "en"
            else "У тебя пока нет сохраненного ника. Нажми ✅ Verification."
        )
        await message.answer(text, reply_markup=await get_keyboard_for_user(message.from_user.id))


@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    lang = await get_lang(message.from_user.id)
    if lang == "en":
        text = (
            "Commands:\n"
            "/start - open main menu\n"
            "/leaderboard - open leaderboard\n"
            "/settings - open settings\n"
            "/language - change language\n"
            "/help - show this help"
        )
    else:
        text = (
            "Команды:\n"
            "/start - открыть главное меню\n"
            "/leaderboard - открыть таблицу лидеров\n"
            "/settings - открыть настройки\n"
            "/language - сменить язык\n"
            "/help - показать помощь"
        )
    await message.answer(text, reply_markup=await get_keyboard_for_user(message.from_user.id))


@dp.message(Command("leaderboard"))
async def leaderboard_cmd(message: types.Message):
    lang = await get_lang(message.from_user.id)
    host = urlparse(LEADERBOARD_URL).hostname
    if not host:
        await message.answer(
            tr(
                lang,
                "Leaderboard URL is invalid in .env. Contact admin.",
                "В .env указан неверный LEADERBOARD_URL. Обратитесь к администратору.",
            ),
            reply_markup=await get_keyboard_for_user(message.from_user.id),
        )
        return

    text = tr(lang, "Open leaderboard:", "Открыть таблицу лидеров:")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=tr(lang, "🏆 Open", "🏆 Открыть"), web_app=WebAppInfo(url=LEADERBOARD_URL))]
        ]
    )
    await message.answer(text, reply_markup=kb)

# =========================
# VERIFICATION FLOW
# =========================
@dp.message(lambda m: button_is(m.text, "verification"))
async def start_verification(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    lang = await get_lang(user_id)

    if not await get_user_language(user_id):
        await state.set_state(Form.waiting_for_language)
        await message.answer(
            f"{_text('ru', 'choose_lang')}\n{_text('en', 'choose_lang')}",
            reply_markup=kb_language_select(),
        )
        return

    if await is_user_verified(user_id):
        await message.answer(
            "✅ You are already verified." if lang == "en" else "✅ Ты уже верифицирован.",
            reply_markup=await get_keyboard_for_user(user_id),
        )
        return

    await message.answer(
        (
            "✅ Verification\n\n"
            "Step 0/2\n"
            "Send your game nickname and UID in ONE message:\n"
            "`nickname UID`\n\n"
            "Example:\n"
            "`My Cool Nick ABCD1234`"
            if lang == "en"
            else
            "✅ Верификация\n\n"
            "Шаг 0/2\n"
            "Отправь игровой ник и UID ОДНИМ сообщением:\n"
            "`nickname UID`\n\n"
            "Пример:\n"
            "`My Cool Nick ABCD1234`"
        ),
        parse_mode="Markdown",
        reply_markup=await get_keyboard_for_user(user_id),
    )
    await state.set_state(Verify.wait_nick_uid)

@dp.message(Verify.wait_nick_uid)
async def verify_get_nick_uid(message: types.Message, state: FSMContext):
    lang = await get_lang(message.from_user.id)
    parsed = parse_nick_uid(message.text or "")
    if not parsed:
        await message.answer(
            tr(
                lang,
                "❌ Wrong format.\nSend in ONE message:\n`nickname UID`\n\nExample:\n`My Cool Nick ABCD1234`",
                "❌ Неверный формат.\nОтправь ОДНИМ сообщением:\n`nickname UID`\n\nПример:\n`My Cool Nick ABCD1234`",
            ),
            parse_mode="Markdown",
        )
        return

    game_name, game_uid = parsed

    ok = await save_user_name(message.from_user.id, game_name)
    if not ok:
        await message.answer(
            tr(
                lang,
                "❌ This nickname is already taken in the database.\nPlease choose a unique nickname and try again.",
                "❌ Этот ник уже занят в базе.\nВыбери другой ник и попробуй снова.",
            ),
            reply_markup=await get_keyboard_for_user(message.from_user.id),
        )
        return

    code_word = generate_code_word()
    await state.update_data(game_name=game_name, game_uid=game_uid, code_word=code_word)

    await message.answer(
        tr(
            lang,
            "Step 1/2\nSend SCREENSHOT #1: your game profile (nickname + UID must be visible).",
            "Шаг 1/2\nОтправь СКРИН #1: профиль в игре (ник + UID должны быть видны).",
        )
    )
    await message.answer(
        tr(
            lang,
            f"🔐 Your code word:\n`{code_word}`\n\nWrite this code word in the in-game chat.\nAfter that, send SCREENSHOT #2 (chat).",
            f"🔐 Твое кодовое слово:\n`{code_word}`\n\nНапиши его в игровом чате.\nПосле этого отправь СКРИН #2 (чат).",
        ),
        parse_mode="Markdown",
    )
    await state.set_state(Verify.wait_profile)

@dp.message(Verify.wait_profile, F.photo)
async def verify_get_profile(message: types.Message, state: FSMContext):
    lang = await get_lang(message.from_user.id)
    file_id = message.photo[-1].file_id
    data = await state.get_data()
    code_word = data.get("code_word")

    await state.update_data(profile_file_id=file_id)

    await message.answer(
        tr(
            lang,
            "✅ Screenshot #1 received.\n\n"
            f"Now write this code word in the in-game chat:\n`{code_word}`\n\n"
            "Step 2/2\nSend SCREENSHOT #2: the chat where the code word is visible.",
            "✅ Скрин #1 получен.\n\n"
            f"Теперь напиши кодовое слово в игровом чате:\n`{code_word}`\n\n"
            "Шаг 2/2\nОтправь СКРИН #2: чат, где видно кодовое слово.",
        ),
        parse_mode="Markdown",
    )
    await state.set_state(Verify.wait_chat)

@dp.message(Verify.wait_chat, F.photo)
async def verify_get_chat(message: types.Message, state: FSMContext):
    lang = await get_lang(message.from_user.id)
    data = await state.get_data()
    profile_file_id = data.get("profile_file_id")
    code_word = data.get("code_word")
    game_name = data.get("game_name")
    game_uid = data.get("game_uid")

    chat_file_id = message.photo[-1].file_id

    if not all([profile_file_id, code_word, game_name, game_uid]):
        await message.answer(
            tr(
                lang,
                "❌ Please restart verification and follow the steps again.",
                "❌ Перезапусти верификацию и пройди шаги заново.",
            )
        )
        await state.clear()
        return

    req_id = await create_verification_request(
        user_id=message.from_user.id,
        tg_username=message.from_user.username,
        game_name=game_name,
        game_uid=game_uid,
        code_word=code_word,
        profile_file_id=profile_file_id,
        chat_file_id=chat_file_id,
    )

    await message.answer(
        tr(lang, "⏳ Request sent to operator. Please wait.", "⏳ Заявка отправлена оператору. Ожидай."),
        reply_markup=await get_keyboard_for_user(message.from_user.id),
    )
    await state.clear()

    uname = f"@{message.from_user.username}" if message.from_user.username else "вЂ”"
    text = (
        f"рџ†• Verification request #{req_id}\n"
        f"tg_id: {message.from_user.id}\n"
        f"tg username: {uname}\n"
        f"game name: {game_name}\n"
        f"game UID: {game_uid}\n"
        f"code word: {code_word}"
    )

    for op_id in OPERATORS:
        await bot.send_message(op_id, text, reply_markup=operator_kb(req_id))
        await bot.send_photo(op_id, profile_file_id, caption="Screenshot #1 (profile)")
        await bot.send_photo(op_id, chat_file_id, caption="Screenshot #2 (chat)")

@dp.callback_query(F.data.startswith("vr_ok|"))
async def vr_ok(callback: types.CallbackQuery):
    lang = await get_lang(callback.from_user.id)
    if callback.from_user.id not in OPERATORS:
        await callback.answer(tr(lang, "No access.", "Нет доступа."))
        return

    req_id = int((callback.data or "").split("|")[1])
    row = await get_verification_request(req_id)
    if not row:
        await callback.answer(tr(lang, "Request not found.", "Заявка не найдена."))
        return

    (_id, user_id, _tg_username, status, game_name, game_uid, *_rest) = row

    if status != "open":
        await callback.answer(tr(lang, "Already processed.", "Уже обработано."))
        return

    try:
        await upsert_verified_account(user_id, game_name, game_uid, callback.from_user.id)
    except Exception as e:
        await callback.answer(tr(lang, "DB error.", "Ошибка БД."))
        await callback.message.answer(
            tr(
                lang,
                f"❌ Could not save verified account (UID may be taken): {e}",
                f"❌ Не удалось сохранить верифицированный аккаунт (UID может быть занят): {e}",
            )
        )
        return

    await set_verification_request_status(req_id, "approved", callback.from_user.id)

    try:
        user_lang = await get_lang(user_id)
        await bot.send_message(
            user_id,
            tr(
                user_lang,
                f"✅ Verification approved.\nNick: {game_name}\nUID: {game_uid}",
                f"✅ Верификация одобрена.\nНик: {game_name}\nUID: {game_uid}",
            ),
            reply_markup=await get_keyboard_for_user(user_id),
        )
    except Exception:
        pass

    await callback.answer(tr(lang, "Approved", "Одобрено"))
    await callback.message.answer(tr(lang, f"✅ Request #{req_id} approved.", f"✅ Заявка #{req_id} одобрена."))

@dp.callback_query(F.data.startswith("vr_no|"))
async def vr_no(callback: types.CallbackQuery):
    lang = await get_lang(callback.from_user.id)
    if callback.from_user.id not in OPERATORS:
        await callback.answer(tr(lang, "No access.", "Нет доступа."))
        return

    req_id = int((callback.data or "").split("|")[1])
    row = await get_verification_request(req_id)
    if not row:
        await callback.answer(tr(lang, "Request not found.", "Заявка не найдена."))
        return

    user_id = row[1]
    status = row[3]
    if status != "open":
        await callback.answer(tr(lang, "Already processed.", "Уже обработано."))
        return

    await set_verification_request_status(req_id, "rejected", callback.from_user.id, reject_reason="bad screenshots")

    user_lang = await get_lang(user_id)
    try:
        await bot.send_message(
            user_id,
            tr(
                user_lang,
                "❌ Verification rejected.\n"
                "Please press ✅ Verification and try again.\n\n"
                "You must send:\n"
                "1) Profile screenshot (nickname + UID visible)\n"
                "2) Chat screenshot with the code word visible",
                "❌ Верификация отклонена.\n"
                "Нажми ✅ Verification и попробуй еще раз.\n\n"
                "Нужно отправить:\n"
                "1) Скрин профиля (видны ник и UID)\n"
                "2) Скрин чата с кодовым словом",
            ),
            reply_markup=await get_keyboard_for_user(user_id),
        )
    except Exception:
        pass

    await callback.answer(tr(lang, "Rejected", "Отклонено"))
    await callback.message.answer(tr(lang, f"❌ Request #{req_id} rejected.", f"❌ Заявка #{req_id} отклонена."))

# =========================
# CHANGE NAME
# =========================
@dp.message(lambda m: button_is(m.text, "change_name"))
async def change_name_request_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    lang = await get_lang(user_id)

    if not await is_user_verified(user_id):
        await message.answer(
            tr(
                lang,
                "❌ You must be verified to request name change.\nPress ✅ Verification.",
                "❌ Для смены ника нужна верификация.\nНажми ✅ Verification.",
            ),
            reply_markup=await kb_name_edit(user_id),
        )
        return

    if await user_in_any_team(user_id):
        await message.answer(
            tr(
                lang,
                "⚠️ You can't change name during an active match.",
                "⚠️ Нельзя менять ник во время активного матча.",
            ),
            reply_markup=await get_keyboard_for_user(user_id),
        )
        return

    if await has_open_name_change_request(user_id):
        await message.answer(
            tr(
                lang,
                "⏳ You already have an open name change request. Please wait for operator.",
                "⏳ У тебя уже есть открытая заявка на смену ника. Дождись оператора.",
            ),
            reply_markup=await get_keyboard_for_user(user_id),
        )
        return

    await state.clear()
    await state.set_state(Form.change_name_wait_new)
    await message.answer(
        tr(
            lang,
            "✍️ Send the NEW nickname you want.\nThen send ONE screenshot proof where NEW nickname is visible.",
            "✍️ Отправь НОВЫЙ ник, который хочешь установить.\nПотом отправь ОДИН скрин, где виден новый ник.",
        ),
        reply_markup=await kb_name_edit(user_id),
    )

@dp.message(
    StateFilter(Form.change_name_wait_new, Form.change_name_wait_photo),
    lambda m: (m.text or "").strip() in {"❌ Cancel name edit", "❌ Отменить смену имени"},
)
async def cancel_name_edit(message: types.Message, state: FSMContext):
    await state.clear()
    lang = await get_lang(message.from_user.id)
    await message.answer(
        tr(lang, "✅ Name edit canceled.", "✅ Смена ника отменена."),
        reply_markup=await get_keyboard_for_user(message.from_user.id),
    )


@dp.message(Form.change_name_wait_new)
async def change_name_new_input(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    lang = await get_lang(user_id)
    new_name = (message.text or "").strip()

    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9 _\-\|]{2,32}", new_name):
        await message.answer(
            tr(
                lang,
                "Invalid name. Use 2-32 letters/numbers/spaces and _ - |",
                "Некорректный ник. Используй 2-32 символа: буквы/цифры/пробелы и _ - |",
            )
        )
        return

    await state.update_data(new_name=new_name)
    await state.set_state(Form.change_name_wait_photo)

    await message.answer(
        tr(
            lang,
            "📸 Now send ONE screenshot proof (photo) where your NEW nickname is visible.\nWithout screenshot operator will not approve.",
            "📸 Теперь отправь ОДИН скрин-доказательство (фото), где виден новый ник.\nБез скрина оператор не одобрит.",
        ),
        reply_markup=await kb_name_edit(user_id),
    )

@dp.message(Form.change_name_wait_photo, F.photo)
async def change_name_photo_input(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    lang = await get_lang(user_id)
    data = await state.get_data()
    new_name = (data.get("new_name") or "").strip()

    if not new_name:
        await message.answer(
            tr(
                lang,
                "❌ Please restart: press ✏️ Change name again.",
                "❌ Перезапусти: нажми ✏️ Change name еще раз.",
            ),
            reply_markup=await get_keyboard_for_user(user_id),
        )
        await state.clear()
        return

    screenshot_file_id = message.photo[-1].file_id
    old_name = await get_user_name(user_id)

    req_id = await create_name_change_request(user_id, old_name, new_name, screenshot_file_id)

    await message.answer(
        tr(
            lang,
            "⏳ Name change request sent to operator. Please wait.",
            "⏳ Заявка на смену ника отправлена оператору. Ожидай.",
        ),
        reply_markup=await get_keyboard_for_user(user_id),
    )
    await state.clear()

    uname = f"@{message.from_user.username}" if message.from_user.username else "вЂ”"
    text = (
        f"рџ“ќ Name change request #{req_id}\n"
        f"tg_id: {user_id}\n"
        f"tg username: {uname}\n"
        f"old name: {old_name or 'вЂ”'}\n"
        f"new name: {new_name}\n"
        f"рџ“ё Screenshot attached below"
    )

    for op_id in OPERATORS:
        await bot.send_message(op_id, text, reply_markup=name_change_op_kb(req_id))
        await bot.send_photo(op_id, screenshot_file_id, caption="Name change proof screenshot")

@dp.message(Form.change_name_wait_photo)
async def change_name_waiting_photo_text(message: types.Message, state: FSMContext):
    lang = await get_lang(message.from_user.id)
    await message.answer(
        tr(
            lang,
            "📸 Please send a screenshot (photo). Text is not accepted.",
            "📸 Отправь скриншот (фото). Текст не принимается.",
        )
    )

@dp.callback_query(F.data.startswith("cn_ok|"))
async def cn_ok(callback: types.CallbackQuery):
    lang = await get_lang(callback.from_user.id)
    if callback.from_user.id not in OPERATORS:
        await callback.answer(tr(lang, "No access.", "Нет доступа."))
        return

    req_id = int((callback.data or "").split("|")[1])
    row = await get_name_change_request(req_id)
    if not row:
        await callback.answer(tr(lang, "Request not found.", "Заявка не найдена."))
        return

    _id, user_id, old_name, new_name, screenshot_file_id, status, *_rest = row
    if status != "open":
        await callback.answer(tr(lang, "Already processed.", "Уже обработано."))
        return

    ok = await save_user_name(user_id, new_name)
    if not ok:
        await callback.message.answer(tr(lang, "❌ Can't approve: this nickname is already taken in DB.", "❌ Невозможно одобрить: этот ник уже занят в БД."))
        await callback.answer(tr(lang, "Error", "Ошибка"))
        return

    await set_name_change_request_status(req_id, "approved", callback.from_user.id)

    try:
        user_lang = await get_lang(user_id)
        await bot.send_message(
            user_id,
            tr(
                user_lang,
                f"✅ Name change approved.\nNew name: {new_name}",
                f"✅ Смена ника одобрена.\nНовый ник: {new_name}",
            ),
            reply_markup=await get_keyboard_for_user(user_id),
        )
    except Exception:
        pass

    await callback.answer(tr(lang, "Approved", "Одобрено"))
    await callback.message.answer(tr(lang, f"✅ Name change request #{req_id} approved.", f"✅ Заявка на смену имени #{req_id} одобрена."))

@dp.callback_query(F.data.startswith("cn_no|"))
async def cn_no(callback: types.CallbackQuery):
    lang = await get_lang(callback.from_user.id)
    if callback.from_user.id not in OPERATORS:
        await callback.answer(tr(lang, "No access.", "Нет доступа."))
        return

    req_id = int((callback.data or "").split("|")[1])
    row = await get_name_change_request(req_id)
    if not row:
        await callback.answer(tr(lang, "Request not found.", "Заявка не найдена."))
        return

    _id, user_id, old_name, new_name, screenshot_file_id, status, *_rest = row
    if status != "open":
        await callback.answer(tr(lang, "Already processed.", "Уже обработано."))
        return

    await set_name_change_request_status(req_id, "rejected", callback.from_user.id, reject_reason="operator rejected")

    try:
        user_lang = await get_lang(user_id)
        await bot.send_message(
            user_id,
            tr(
                user_lang,
                "❌ Name change rejected by operator.\nIf needed, send a new request via ✏️ Change name.",
                "❌ Смена имени отклонена оператором.\nПри необходимости отправь новую заявку через ✏️ Change name.",
            ),
            reply_markup=await get_keyboard_for_user(user_id),
        )
    except Exception:
        pass

    await callback.answer(tr(lang, "Rejected", "Отклонено"))
    await callback.message.answer(tr(lang, f"❌ Name change request #{req_id} rejected.", f"❌ Заявка на смену имени #{req_id} отклонена."))

# =========================
# QUEUE ENTRY (single source of truth)
# =========================
async def enter_search(user_id: int):
    lang = await get_lang(user_id)
    if not await is_user_verified(user_id):
        await safe_send(
            user_id,
            (
                "❌ You are not verified.\nPress ✅ Verification and complete the steps."
                if lang == "en"
                else "❌ Ты не верифицирован.\nНажми ✅ Verification и пройди шаги."
            ),
            reply_markup=await get_keyboard_for_user(user_id),
        )
        return
    if is_banned_sync(user_id):
        remaining = get_ban_remaining(user_id)
        minutes = int(remaining.total_seconds() // 60) if remaining else 0
        await safe_send(
            user_id,
            (
                f"🚫 You are banned for {minutes} more minutes."
                if lang == "en"
                else f"🚫 Бан еще на {minutes} минут."
            ),
            reply_markup=await get_keyboard_for_user(user_id),
        )
        return
    user_name = await get_user_name(user_id)
    if not user_name:
        await safe_send(
            user_id,
            (
                "❌ You have no name saved. Press ✅ Verification."
                if lang == "en"
                else "❌ У тебя нет сохраненного ника. Нажми ✅ Verification."
            ),
            reply_markup=await get_keyboard_for_user(user_id),
        )
        return
    if user_platform.get(user_id) not in ('pc', 'android'):
        waiting_platform.add(user_id)
        await safe_send(
            user_id,
            "🕹 Before we start, choose your platform:" if lang == "en" else "🕹 Перед стартом выбери платформу:",
            reply_markup=platform_keyboard(),
        )
        return
    if await user_in_any_team(user_id):
        await safe_send(
            user_id,
            (
                "⚠️ You are already in an active match.\nWait until it finishes."
                if lang == "en"
                else "⚠️ Ты уже в активном матче.\nДождись завершения."
            ),
            reply_markup=await get_keyboard_for_user(user_id),
        )
        return
    if user_id in search_queue:
        await safe_send(
            user_id,
            "⏳ You are already in the search queue." if lang == "en" else "⏳ Ты уже в очереди поиска.",
            reply_markup=await get_keyboard_for_user(user_id),
        )
        return
    search_queue.add(user_id)
    await send_or_update_queue_status(user_id)
    await update_queue_status_for_all()
    await try_make_matches_from_queue()

@dp.message(lambda m: button_is(m.text, "find_match"))
async def find_match(message: types.Message):
    user_id = message.from_user.id
    await enter_search(user_id)

@dp.message(lambda m: button_is(m.text, "cancel_search"))

async def cancel_search(message: types.Message):
    user_id = message.from_user.id
    lang = await get_lang(user_id)
    if user_id in search_queue:
        search_queue.discard(user_id)
        await send_or_update_queue_status(user_id)
        await update_queue_status_for_all()
        await message.answer(
            "✅ Search canceled." if lang == "en" else "✅ Поиск отменен.",
            reply_markup=await get_keyboard_for_user(user_id),
        )
    else:
        await message.answer(
            "You are not in the queue." if lang == "en" else "Тебя нет в очереди.",
            reply_markup=await get_keyboard_for_user(user_id),
        )

# =========================
# CONFIRM PARTICIPATION
# =========================

# =========================
# CONFIRM PARTICIPATION
# =========================
@dp.callback_query(F.data.startswith("confirm|"))
async def confirm_participation(callback: types.CallbackQuery):
    lang = await get_lang(callback.from_user.id)
    parts = (callback.data or "").split("|", 2)
    if len(parts) != 3:
        await callback.answer(tr(lang, "Invalid data.", "Неверные данные."))
        return

    _, team_id, player_name = parts

    if team_id not in active_teams or player_name not in active_teams[team_id]:
        await callback.answer(tr(lang, "Team not found or already removed.", "Команда не найдена или уже удалена."))
        return

    # if already confirmed
    if player_name in team_confirmed.get(team_id, set()):
        await callback.answer(tr(lang, "✅ Already confirmed.", "✅ Уже подтверждено."))
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    # must have an active timer for confirmation
    if team_id not in team_timers or player_name not in team_timers.get(team_id, {}):
        await callback.answer(tr(lang, "Time is up or confirmation is not required.", "Время вышло или подтверждение не требуется."))
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    now = now_utc()
    if now >= team_timers[team_id][player_name]:
        team_timers[team_id].pop(player_name, None)
        if player_name in active_teams[team_id]:
            active_teams[team_id].remove(player_name)

        uid = callback.from_user.id
        banned_users[uid] = now + timedelta(hours=BAN_HOURS)

        await callback.answer(tr(lang, "⏰ Time is up. You were removed.", "⏰ Время вышло. Ты удален из матча."))
        window = CONFIRM_MINUTES if team_phase.get(team_id, 1) == 1 else SECOND_CONFIRM_MINUTES
        await update_team_confirm_messages(team_id, window)
        return

    # confirm
    team_timers[team_id].pop(player_name, None)
    team_confirmed.setdefault(team_id, set()).add(player_name)

    try:
        await callback.answer(tr(lang, "✅ Participation confirmed!", "✅ Участие подтверждено!"))
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    window = CONFIRM_MINUTES if team_phase.get(team_id, 1) == 1 else SECOND_CONFIRM_MINUTES
    await update_team_confirm_messages(team_id, window)

    # Start early ONLY if everyone (8) confirmed; otherwise wait for window expiry.
    players_now = active_teams.get(team_id, [])
    confirmed_now = team_confirmed.get(team_id, set())
    confirmed_count = len([p for p in players_now if p in confirmed_now])
    if confirmed_count >= TARGET_CONFIRMATIONS and team_id not in started_matches:
        started_matches.add(team_id)
        team_deadline.pop(team_id, None)
        await start_match(team_id)


# =========================
@dp.callback_query(F.data.startswith("change_captain|"))
async def change_captain(callback: types.CallbackQuery):
    lang = await get_lang(callback.from_user.id)
    _, team_id = (callback.data or "").split("|", 1)

    if team_id not in active_teams:
        await callback.answer(tr(lang, "Team not found.", "Команда не найдена."))
        return

    user_name = await get_user_name(callback.from_user.id)
    if team_captains.get(team_id) != user_name:
        await callback.answer(tr(lang, "Only the captain can transfer leadership.", "Только капитан может передать лидерство."))
        return

    players = active_teams[team_id]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=p, callback_data=f"set_captain|{team_id}|{p}")]
            for p in players
            if p != user_name
        ]
    )

    await callback.message.answer(tr(lang, "Choose a new captain:", "Выбери нового капитана:"), reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("set_captain|"))
async def set_captain(callback: types.CallbackQuery):
    lang = await get_lang(callback.from_user.id)
    parts = (callback.data or "").split("|", 2)
    if len(parts) != 3:
        await callback.answer(tr(lang, "Invalid data.", "Неверные данные."))
        return

    _, team_id, new_captain = parts

    if team_id not in active_teams:
        await callback.answer(tr(lang, "Team not found.", "Команда не найдена."))
        return

    old_captain = team_captains.get(team_id)
    user_name = await get_user_name(callback.from_user.id)

    if user_name != old_captain:
        await callback.answer(tr(lang, "Only current captain can transfer leadership.", "Только текущий капитан может передать лидерство."))
        return

    team_captains[team_id] = new_captain

    new_id = await get_user_id_by_name(new_captain)
    old_id = await get_user_id_by_name(old_captain) if old_captain else None

    try:
        if old_id:
            old_lang = await get_lang(old_id)
            await bot.send_message(old_id, tr(old_lang, "You transferred the captain role.", "Ты передал роль капитана."))
    except Exception:
        pass

    if new_id:
        try:
            new_lang = await get_lang(new_id)
            await bot.send_message(new_id, tr(new_lang, "🎖 You are now the team captain!", "🎖 Теперь ты капитан команды!"))
            await bot.send_message(new_id, tr(new_lang, "🎮 Captain panel:", "🎮 Панель капитана:"), reply_markup=captain_panel_keyboard(team_id))
        except Exception:
            pass

    await callback.answer(tr(lang, "Captain transferred!", "Капитан передан!"))

# =========================
# SEND CODE TO TEAM
# =========================

    # =========================
    # NO CODE VOTE -> TRANSFER CAPTAIN
    # =========================
    @dp.callback_query(F.data.startswith("nocode|"))
    async def vote_no_code(callback: types.CallbackQuery):
        lang = await get_lang(callback.from_user.id)
        parts = (callback.data or "").split("|", 1)
        if len(parts) != 2:
            await callback.answer(tr(lang, "Invalid", "Неверно"))
            return
        team_id = parts[1]
        if team_id not in active_teams:
            await callback.answer(tr(lang, "Match not found.", "Матч не найден."))
            return

        uid = callback.from_user.id
        user_name = await get_user_name(uid)
        if not user_name or user_name not in active_teams.get(team_id, []):
            await callback.answer(tr(lang, "You are not in this match.", "Тебя нет в этом матче."))
            return

        votes = team_no_code_votes.setdefault(team_id, set())
        if uid in votes:
            await callback.answer(tr(lang, "Already voted.", "Ты уже голосовал."))
            return

        cap_name = team_captains.get(team_id)
        if cap_name and user_name == cap_name:
            await callback.answer(tr(lang, "Captain can't vote.", "Капитан не может голосовать."))
            return

        votes.add(uid)

        total_players = len(active_teams.get(team_id, []))
        needed = total_players // 2 + 1
        await callback.answer(tr(lang, f"Voted ({len(votes)}/{needed})", f"Голос принят ({len(votes)}/{needed})"))

        if len(votes) < needed:
            return

        old_captain = team_captains.get(team_id)
        players = active_teams.get(team_id, [])
        candidates = [p for p in players if p != old_captain]
        if not candidates:
            return

        pc_names = []
        for p in candidates:
            pid = await get_user_id_by_name(p)
            if pid and user_platform.get(pid) == "pc":
                pc_names.append(p)

        new_captain = random.choice(pc_names) if pc_names else random.choice(candidates)
        team_captains[team_id] = new_captain
        team_no_code_votes[team_id] = set()

        # notify all players
        for p in players:
            pid = await get_user_id_by_name(p)
            if not pid:
                continue
            try:
                if p == new_captain:
                    p_lang = await get_lang(pid)
                    await bot.send_message(
                        pid,
                        tr(
                            p_lang,
                            "🎖 You are now the team captain!\n\nUse the captain panel below.\nWhen the match ends, press 🏁 Open results so everyone can send screenshots.",
                            "🎖 Теперь ты капитан команды!\n\nИспользуй панель капитана ниже.\nКогда матч закончится, нажми 🏁 Open results, чтобы все отправили скриншоты.",
                        )
                    )
                    await bot.send_message(pid, tr(p_lang, "🛠 Captain panel:", "🛠 Панель капитана:"), reply_markup=captain_panel_keyboard(team_id))
                elif p == old_captain:
                    p_lang = await get_lang(pid)
                    await bot.send_message(pid, tr(p_lang, "⚠️ Captain role was transferred (No code vote).", "⚠️ Роль капитана передана (голосование No code)."))
                else:
                    p_lang = await get_lang(pid)
                    await bot.send_message(pid, tr(p_lang, f"⚠️ Captain changed to: {new_captain} (No code vote).", f"⚠️ Капитан изменен на: {new_captain} (голосование No code)."))
            except Exception:
                pass

        # refresh match messages so buttons reflect new captain
        try:
            current_minutes = CONFIRM_MINUTES if team_phase.get(team_id, 1) == 1 else SECOND_CONFIRM_MINUTES
            await update_team_confirm_messages(team_id, current_minutes)
        except Exception:
            pass

@dp.callback_query(F.data.startswith("send_code_"))
async def prompt_for_code(callback: types.CallbackQuery, state: FSMContext):
    lang = await get_lang(callback.from_user.id)
    raw = callback.data or ""
    team_id = raw[len("send_code_"):]

    if team_id not in active_teams:
        await callback.answer(tr(lang, "Team not found or expired.", "Команда не найдена или уже завершена."))
        return

    user_name = await get_user_name(callback.from_user.id)
    if team_captains.get(team_id) != user_name:
        await callback.answer(tr(lang, "Only the captain can send the code.", "Только капитан может отправить код."))
        return

    await state.update_data(team_id=team_id)
    await callback.message.answer(
        tr(
            lang,
            "Enter a 3-digit code to send to your team (e.g. 123):",
            "Введи 3-значный код для команды (например, 123):",
        )
    )
    await state.set_state(Form.waiting_for_code)
    await callback.answer()

@dp.message(Form.waiting_for_code)
async def send_code_to_team(message: types.Message, state: FSMContext):
    lang = await get_lang(message.from_user.id)
    code = (message.text or "").strip()
    if not code.isdigit() or len(code) != 3:
        await message.answer(tr(lang, "Please enter exactly 3 digits (e.g., 123).", "Введи ровно 3 цифры (например, 123)."))
        return

    data = await state.get_data()
    team_id = data.get("team_id")

    team_players = active_teams.get(team_id)
    if not team_players:
        await message.answer(tr(lang, "Team not found. Maybe match finished or bot restarted.", "Команда не найдена. Возможно матч завершен или бот перезапускался."))
        await state.clear()
        return

    for player_name in team_players:
        uid = await get_user_id_by_name(player_name)
        if uid:
            try:
                u_lang = await get_lang(uid)
                await bot.send_message(uid, tr(u_lang, f"🔐 Team code: {code}", f"🔐 Код команды: {code}"))
            except Exception:
                pass

    await message.answer(
        tr(lang, f"✅ Code {code} sent.", f"✅ Код {code} отправлен."),
        reply_markup=await get_keyboard_for_user(message.from_user.id),
    )
    await state.clear()

# =========================
# RESULTS: captain sends screenshot -> AI -> FINALIZE
# =========================
@dp.message(StateFilter(None), F.photo)
async def handle_match_photo(message: types.Message):
    user_id = message.from_user.id
    lang = await get_lang(user_id)
    user_name = await get_user_name(user_id)

    if not user_name:
        await message.answer(
            tr(lang, "❌ You have no name saved. Press ✅ Verification.", "❌ У тебя нет сохраненного ника. Нажми ✅ Verification."),
            reply_markup=await get_keyboard_for_user(user_id),
        )
        return

    team_id = await find_team_id_by_user_id(user_id)
    if not team_id:
        await message.answer(
            tr(lang, "❌ You are not in an active match.", "❌ Ты не в активном матче."),
            reply_markup=await get_keyboard_for_user(user_id),
        )
        return

    if team_id in match_results_sent:
        await message.answer(tr(lang, "⚠ Results for this match were already submitted.", "⚠ Результаты этого матча уже отправлены."))
        return

    captain_name = team_captains.get(team_id)
    if not captain_name or captain_name != user_name:
        await message.answer(tr(lang, "❌ Only the captain can submit results.", "❌ Только капитан может отправить результаты."))
        return

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    image_stream = await bot.download_file(file.file_path)
    image_bytes = image_stream.read()

    await message.answer(
        tr(
            lang,
            "📷 Processing image with AI...\nExtracting top-8 nicknames...",
            "📷 Обрабатываю изображение через AI...\nИзвлекаю топ-8 ников...",
        )
    )

    try:
        parsed = extract_player_names(image_bytes)
    except Exception as e:
        await message.answer(tr(lang, f"❌ AI parse failed: {type(e).__name__}: {e}", f"❌ Ошибка распознавания AI: {type(e).__name__}: {e}"))
        return

    names = (parsed.players or [])[:8]
    if len(names) < 8:
        txt = (
            tr(lang, f"⚠ Detected only {len(names)} players (need 8).\n\n", f"⚠ Найдено только {len(names)} игроков (нужно 8).\n\n")
            + ("\n".join([f"{i+1}. {n}" for i, n in enumerate(names)]) if names else "вЂ”")
        )
        await message.answer(txt)
        if parsed.notes:
            await message.answer(tr(lang, "AI notes:\n- ", "Заметки AI:\n- ") + "\n- ".join(parsed.notes))
        return

    medals = ["рџҐ‡", "рџҐ€", "рџҐ‰"]
    lines = [tr(lang, "✅ Detected results:", "✅ Найденные результаты:")]
    for i, n in enumerate(names, start=1):
        prefix = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{prefix} {n}")

    await message.answer("\n".join(lines))
    await message.answer(tr(lang, "✅ Finalizing results...", "✅ Завершаю обработку результатов..."))

    try:
        await finalize_results_direct(team_id, names)
    except Exception as e:
        await message.answer(tr(lang, f"❌ Finalize failed: {type(e).__name__}: {e}", f"❌ Ошибка финализации: {type(e).__name__}: {e}"))
        return

    if parsed.notes:
        await message.answer(tr(lang, "AI notes:\n- ", "Заметки AI:\n- ") + "\n- ".join(parsed.notes))

# =========================
@dp.callback_query(F.data.startswith("platform|"))
async def platform_selected_start_search(callback: types.CallbackQuery):
    lang = await get_lang(callback.from_user.id)
    parts = (callback.data or "").split("|", 1)
    if len(parts) != 2:
        await callback.answer(tr(lang, "Invalid", "Неверно"))
        return
    plat = parts[1]
    if plat not in ("pc", "android"):
        await callback.answer(tr(lang, "Invalid", "Неверно"))
        return

    uid = callback.from_user.id
    user_platform[uid] = plat
    waiting_platform.discard(uid)

    # confirm + start
    try:
        await callback.message.edit_text(
            tr(
                lang,
                f"✅ Platform saved: {'PC' if plat=='pc' else 'Android'}\n🔍 Starting search...",
                f"✅ Платформа сохранена: {'PC' if plat=='pc' else 'Android'}\n🔍 Запускаю поиск...",
            )
        )
    except Exception:
        pass

    await callback.answer(tr(lang, "Saved", "Сохранено"))
    await enter_search(uid)


@dp.callback_query()
async def fallback_callback(callback: types.CallbackQuery):
    lang = await get_lang(callback.from_user.id)
    await callback.answer(tr(lang, "Unknown action.", "Неизвестное действие."), show_alert=False)
    print(f"[fallback_callback] from={callback.from_user.id} data={callback.data!r}", flush=True)


@dp.message()
async def fallback_message(message: types.Message):
    lang = await get_lang(message.from_user.id)
    text = tr(
        lang,
        "Command not recognized. Use /start to open the menu.",
        "Команда не распознана. Используйте /start, чтобы открыть меню.",
    )
    await message.answer(text, reply_markup=await get_keyboard_for_user(message.from_user.id))
    print(f"[fallback_message] from={message.from_user.id} text={message.text!r}", flush=True)


async def main():
    while True:
        try:
            print("[polling] starting...", flush=True)
            await dp.start_polling(bot)
            break
        except TelegramNetworkError as e:
            print(f"[network] Telegram API unavailable, retry in 10s: {e}", flush=True)
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
