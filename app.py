"""
Life Changing Task — бот + Telegram Mini App, с редактируемым списком задач.

Что нового по сравнению с первой версией:
- У каждого пользователя теперь СВОЙ список задач (таблица user_tasks),
  а не общий жёстко зашитый список.
- При первом запуске (/start) список заполняется 9 задачами по умолчанию
  (как в исходной таблице), но дальше их можно редактировать, удалять
  и добавлять свои — максимум MAX_TASKS штук.
- Управление задачами — прямо в Mini App, вкладка "Мои задачи".

Остальное устройство файла — как в предыдущей версии (см. README.md):
FastAPI-сервер + Telegram-бот в одном процессе.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import date, datetime
from urllib.parse import parse_qsl

import aiosqlite
import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MINI_APP_URL = os.environ.get("MINI_APP_URL", "")
DB_PATH = os.environ.get("HABITBOT_DB_PATH", "habitbot.sqlite3")
PORT = int(os.environ.get("PORT", "8000"))

TOTAL_WEEKS = 26
DEFAULT_HOUR = 21
DEFAULT_MINUTE = 0
MAX_TASKS = 10

# Задачи по умолчанию (как в исходной таблице). key, эмодзи, название, подсказка.
DEFAULT_TASKS: list[tuple[str, str, str, str]] = [
    ("sleep", "😴", "Сон", "Отбой до 23:00 / подъём в 7:00"),
    ("sport", "🏋️", "Спорт", "Зал / бег / прогулка"),
    ("cold_shower", "🚿", "Холодный душ", "+ медитация 15 мин"),
    ("reading", "📖", "Чтение", "Книга / подкаст"),
    ("no_sugar", "☕", "Без кофе и сахара", ""),
    ("no_alco", "🍷", "Без алкоголя", "и без курения"),
    ("no_porn", "📵", "Без порно/соцсетей", ""),
    ("save_money", "💰", "Финансы", "Откладывать деньги / крипта"),
    ("love", "❤️", "Скажи близким", "что любишь их"),
]

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()
app = FastAPI()

pending_checkins: dict[int, dict] = {}


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------
async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                start_date TEXT NOT NULL,
                reminder_hour INTEGER NOT NULL DEFAULT 21,
                reminder_minute INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_tasks (
                user_id INTEGER NOT NULL,
                task_key TEXT NOT NULL,
                emoji TEXT NOT NULL DEFAULT '⭐',
                title TEXT NOT NULL,
                hint TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL,
                PRIMARY KEY (user_id, task_key)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS checkins (
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                task_key TEXT NOT NULL,
                done INTEGER NOT NULL,
                PRIMARY KEY (user_id, day, task_key)
            )
            """
        )
        await db.commit()


async def get_user(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def upsert_user(user_id: int, start_date_str: str, hour: int, minute: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, start_date, reminder_hour, reminder_minute)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                reminder_hour = excluded.reminder_hour,
                reminder_minute = excluded.reminder_minute
            """,
            (user_id, start_date_str, hour, minute),
        )
        await db.commit()


async def seed_default_tasks(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        for i, (key, emoji, title, hint) in enumerate(DEFAULT_TASKS):
            await db.execute(
                """
                INSERT OR IGNORE INTO user_tasks (user_id, task_key, emoji, title, hint, position)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, key, emoji, title, hint, i),
            )
        await db.commit()


async def ensure_user(user_id: int) -> dict:
    user = await get_user(user_id)
    if user:
        return user
    start_str = date.today().isoformat()
    await upsert_user(user_id, start_str, DEFAULT_HOUR, DEFAULT_MINUTE)
    await seed_default_tasks(user_id)
    schedule_reminder(user_id, DEFAULT_HOUR, DEFAULT_MINUTE)
    return await get_user(user_id)


async def get_user_tasks(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT task_key, emoji, title, hint, position FROM user_tasks "
            "WHERE user_id = ? ORDER BY position",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def add_user_task(user_id: int, title: str, emoji: str, hint: str) -> str | None:
    tasks = await get_user_tasks(user_id)
    if len(tasks) >= MAX_TASKS:
        return None
    task_key = uuid.uuid4().hex[:10]
    next_position = (max((t["position"] for t in tasks), default=-1)) + 1
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO user_tasks (user_id, task_key, emoji, title, hint, position)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, task_key, emoji or "⭐", title.strip()[:60], hint.strip()[:80], next_position),
        )
        await db.commit()
    return task_key


async def edit_user_task(user_id: int, task_key: str, title: str, emoji: str, hint: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE user_tasks SET title = ?, emoji = ?, hint = ?
            WHERE user_id = ? AND task_key = ?
            """,
            (title.strip()[:60], emoji or "⭐", hint.strip()[:80], user_id, task_key),
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_user_task(user_id: int, task_key: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM user_tasks WHERE user_id = ? AND task_key = ?",
            (user_id, task_key),
        )
        await db.commit()
        return cur.rowcount > 0


async def save_checkin(user_id: int, day_str: str, states: dict[str, bool]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        for key, done in states.items():
            await db.execute(
                """
                INSERT INTO checkins (user_id, day, task_key, done)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, day, task_key) DO UPDATE SET done = excluded.done
                """,
                (user_id, day_str, key, int(done)),
            )
        await db.commit()


async def get_existing_checkin(user_id: int, day_str: str) -> dict[str, bool]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT task_key, done FROM checkins WHERE user_id = ? AND day = ?",
            (user_id, day_str),
        ) as cur:
            rows = await cur.fetchall()
            return {key: bool(done) for key, done in rows}


async def get_all_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_all_checkins(user_id: int) -> list[tuple[str, str, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT day, task_key, done FROM checkins WHERE user_id = ? ORDER BY day",
            (user_id,),
        ) as cur:
            return await cur.fetchall()


# ---------------------------------------------------------------------------
# Подсчёт прогресса (теперь на основе персонального списка задач)
# ---------------------------------------------------------------------------
async def compute_progress(user_id: int, start_date_str: str, task_keys: list[str]) -> dict:
    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    days_elapsed = (date.today() - start_dt).days + 1
    total_days_planned = TOTAL_WEEKS * 7
    current_week = min(max((days_elapsed - 1) // 7 + 1, 1), TOTAL_WEEKS)

    rows = await get_all_checkins(user_id)
    per_task_done: dict[str, int] = {key: 0 for key in task_keys}
    day_totals: dict[str, int] = {}
    days_with_data: set[str] = set()

    week_task_done: dict[int, dict[str, int]] = {}
    week_task_total: dict[int, dict[str, int]] = {}

    for day_str, task_key, done in rows:
        if task_key not in per_task_done:
            continue  # задача с тех пор удалена — в статистику не включаем
        days_with_data.add(day_str)
        day_dt = datetime.strptime(day_str, "%Y-%m-%d").date()
        day_index = (day_dt - start_dt).days
        week_num = day_index // 7 + 1
        if 1 <= week_num <= TOTAL_WEEKS:
            week_task_total.setdefault(week_num, {k: 0 for k in task_keys})
            week_task_done.setdefault(week_num, {k: 0 for k in task_keys})
            week_task_total[week_num][task_key] = week_task_total[week_num].get(task_key, 0) + 1
            if done:
                week_task_done[week_num][task_key] = week_task_done[week_num].get(task_key, 0) + 1
        if done:
            per_task_done[task_key] = per_task_done.get(task_key, 0) + 1
            day_totals[day_str] = day_totals.get(day_str, 0) + 1

    perfect_days = sum(1 for d in days_with_data if day_totals.get(d, 0) == len(task_keys))
    denom = max(len(days_with_data), 1)
    per_task_pct = {key: round(100 * per_task_done.get(key, 0) / denom) for key in task_keys}

    weekly_grid = []
    for week_num in range(1, current_week + 1):
        totals = week_task_total.get(week_num, {})
        dones = week_task_done.get(week_num, {})
        row = {}
        for key in task_keys:
            t = totals.get(key, 0)
            d = dones.get(key, 0)
            row[key] = round(100 * d / t) if t else 0
        weekly_grid.append({"week": week_num, "tasks": row})

    return {
        "days_elapsed": min(days_elapsed, total_days_planned),
        "total_days": total_days_planned,
        "current_week": current_week,
        "total_weeks": TOTAL_WEEKS,
        "days_with_data": len(days_with_data),
        "perfect_days": perfect_days,
        "per_task_pct": per_task_pct,
        "weekly_grid": weekly_grid,
        "finished": days_elapsed > total_days_planned,
    }


def tasks_description(tasks: list[dict]) -> str:
    lines = ["*Твои задачи:*\n"]
    for i, t in enumerate(tasks, start=1):
        hint_part = f" — {t['hint']}" if t["hint"] else ""
        lines.append(f"{i}. {t['emoji']} *{t['title']}*{hint_part}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Проверка подписи Telegram WebApp initData
# ---------------------------------------------------------------------------
def verify_init_data(init_data: str) -> dict | None:
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None
    return pairs


def extract_user_id(pairs: dict) -> int | None:
    user_raw = pairs.get("user")
    if not user_raw:
        return None
    try:
        return int(json.loads(user_raw)["id"])
    except (ValueError, KeyError, TypeError):
        return None


async def authenticate(init_data: str) -> int | None:
    pairs = verify_init_data(init_data)
    if not pairs:
        return None
    return extract_user_id(pairs)


# ---------------------------------------------------------------------------
# FastAPI: страница + API
# ---------------------------------------------------------------------------
@app.get("/")
async def serve_index() -> FileResponse:
    return FileResponse(os.path.join("static", "index.html"))


@app.get("/api/state")
async def api_state(initData: str) -> JSONResponse:
    user_id = await authenticate(initData)
    if not user_id:
        return JSONResponse({"error": "invalid_init_data"}, status_code=401)

    user = await ensure_user(user_id)
    tasks = await get_user_tasks(user_id)
    task_keys = [t["task_key"] for t in tasks]
    today_str = date.today().isoformat()
    today_states = await get_existing_checkin(user_id, today_str)
    progress = await compute_progress(user_id, user["start_date"], task_keys)

    return JSONResponse(
        {
            "tasks": tasks,
            "max_tasks": MAX_TASKS,
            "today": today_str,
            "today_states": {k: v for k, v in today_states.items() if k in task_keys},
            "progress": progress,
        }
    )


@app.post("/api/checkin")
async def api_checkin(request: Request) -> JSONResponse:
    body = await request.json()
    user_id = await authenticate(body.get("initData", ""))
    if not user_id:
        return JSONResponse({"error": "invalid_init_data"}, status_code=401)

    user = await ensure_user(user_id)
    tasks = await get_user_tasks(user_id)
    task_keys = [t["task_key"] for t in tasks]
    states = body.get("states", {})
    clean_states = {k: bool(v) for k, v in states.items() if k in task_keys}
    today_str = date.today().isoformat()
    await save_checkin(user_id, today_str, clean_states)
    progress = await compute_progress(user_id, user["start_date"], task_keys)

    return JSONResponse({"ok": True, "progress": progress})


@app.post("/api/tasks/add")
async def api_tasks_add(request: Request) -> JSONResponse:
    body = await request.json()
    user_id = await authenticate(body.get("initData", ""))
    if not user_id:
        return JSONResponse({"error": "invalid_init_data"}, status_code=401)
    title = (body.get("title") or "").strip()
    if not title:
        return JSONResponse({"error": "empty_title"}, status_code=400)

    await ensure_user(user_id)
    task_key = await add_user_task(user_id, title, body.get("emoji", ""), body.get("hint", ""))
    if not task_key:
        return JSONResponse({"error": "limit_reached"}, status_code=400)
    tasks = await get_user_tasks(user_id)
    return JSONResponse({"ok": True, "tasks": tasks})


@app.post("/api/tasks/edit")
async def api_tasks_edit(request: Request) -> JSONResponse:
    body = await request.json()
    user_id = await authenticate(body.get("initData", ""))
    if not user_id:
        return JSONResponse({"error": "invalid_init_data"}, status_code=401)
    title = (body.get("title") or "").strip()
    task_key = body.get("task_key", "")
    if not title or not task_key:
        return JSONResponse({"error": "bad_request"}, status_code=400)

    ok = await edit_user_task(user_id, task_key, title, body.get("emoji", ""), body.get("hint", ""))
    if not ok:
        return JSONResponse({"error": "not_found"}, status_code=404)
    tasks = await get_user_tasks(user_id)
    return JSONResponse({"ok": True, "tasks": tasks})


@app.post("/api/tasks/delete")
async def api_tasks_delete(request: Request) -> JSONResponse:
    body = await request.json()
    user_id = await authenticate(body.get("initData", ""))
    if not user_id:
        return JSONResponse({"error": "invalid_init_data"}, status_code=401)
    task_key = body.get("task_key", "")
    ok = await delete_user_task(user_id, task_key)
    if not ok:
        return JSONResponse({"error": "not_found"}, status_code=404)
    tasks = await get_user_tasks(user_id)
    return JSONResponse({"ok": True, "tasks": tasks})


app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Бот: текстовый чек-ин (fallback), теперь тоже по персональному списку
# ---------------------------------------------------------------------------
def build_checkin_keyboard(tasks: list[dict], states: dict[str, bool]) -> InlineKeyboardMarkup:
    rows = []
    for t in tasks:
        mark = "✅" if states.get(t["task_key"]) else "⬜"
        rows.append(
            [InlineKeyboardButton(text=f"{mark} {t['emoji']} {t['title']}", callback_data=f"toggle:{t['task_key']}")]
        )
    rows.append([InlineKeyboardButton(text="💾 Сохранить", callback_data="confirm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_checkin(user_id: int) -> None:
    tasks = await get_user_tasks(user_id)
    today_str = date.today().isoformat()
    existing = await get_existing_checkin(user_id, today_str)
    states = {t["task_key"]: existing.get(t["task_key"], False) for t in tasks}
    try:
        if MINI_APP_URL:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📲 Открыть трекер", web_app=WebAppInfo(url=MINI_APP_URL))]
                ]
            )
            msg = await bot.send_message(
                user_id,
                f"Как прошёл день ({today_str})? Открой трекер и отметь, что выполнил:",
                reply_markup=kb,
            )
        else:
            msg = await bot.send_message(
                user_id,
                f"Как прошёл день ({today_str})? Отметь, что выполнил:",
                reply_markup=build_checkin_keyboard(tasks, states),
            )
    except Exception:
        logging.exception("Не удалось отправить чек-ин пользователю %s", user_id)
        return
    pending_checkins[user_id] = {"date": today_str, "states": states, "message_id": msg.message_id}


# ---------------------------------------------------------------------------
# Хендлеры команд бота
# ---------------------------------------------------------------------------
@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id
    existing = await get_user(user_id)
    if existing:
        await message.answer(
            "Ты уже участвуешь в программе! Команды:\n"
            "/app — открыть трекер (Mini App)\n"
            "/today — текстовый чек-ин\n"
            "/progress — прогресс текстом\n"
            "/tasks — список задач\n"
            "/settime ЧЧ:ММ — время напоминания"
        )
        return

    await ensure_user(user_id)
    tasks = await get_user_tasks(user_id)
    text = (
        "Добро пожаловать в *Life Changing Task*!\n\n"
        f"Программа на *26 недель (6 месяцев)*. Каждый день в "
        f"{DEFAULT_HOUR:02d}:{DEFAULT_MINUTE:02d} буду присылать напоминание.\n\n"
        + tasks_description(tasks)
        + "\n\nЗадачи можно менять под себя (добавлять/удалять/редактировать) "
        "прямо в трекере, вкладка «Мои задачи»."
    )
    if MINI_APP_URL:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📲 Открыть трекер", web_app=WebAppInfo(url=MINI_APP_URL))]
            ]
        )
        await message.answer(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await message.answer(text + "\n\nОтметиться — /today", parse_mode="Markdown")


@dp.message(Command("app"))
async def cmd_app(message: Message) -> None:
    if not MINI_APP_URL:
        await message.answer(
            "Mini App пока не подключён (нет MINI_APP_URL на сервере). "
            "Используй /today для текстового чек-ина."
        )
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📲 Открыть трекер", web_app=WebAppInfo(url=MINI_APP_URL))]
        ]
    )
    await message.answer("Жми, чтобы открыть трекер:", reply_markup=kb)


@dp.message(Command("tasks"))
async def cmd_tasks(message: Message) -> None:
    await ensure_user(message.from_user.id)
    tasks = await get_user_tasks(message.from_user.id)
    await message.answer(tasks_description(tasks), parse_mode="Markdown")


@dp.message(Command("today"))
async def cmd_today(message: Message) -> None:
    await ensure_user(message.from_user.id)
    await send_checkin(message.from_user.id)


@dp.message(Command("settime"))
async def cmd_settime(message: Message) -> None:
    user_id = message.from_user.id
    user = await get_user(user_id)
    if not user:
        await message.answer("Сначала запусти программу командой /start.")
        return

    parts = message.text.strip().split()
    if len(parts) != 2 or ":" not in parts[1]:
        await message.answer("Формат: /settime 21:30")
        return
    try:
        hour_str, minute_str = parts[1].split(":")
        hour, minute = int(hour_str), int(minute_str)
        assert 0 <= hour <= 23 and 0 <= minute <= 59
    except (ValueError, AssertionError):
        await message.answer("Не понял время. Формат: /settime 21:30")
        return

    await upsert_user(user_id, user["start_date"], hour, minute)
    schedule_reminder(user_id, hour, minute)
    await message.answer(f"Готово! Теперь буду писать в {hour:02d}:{minute:02d} каждый день.")


@dp.message(Command("progress"))
async def cmd_progress(message: Message) -> None:
    user_id = message.from_user.id
    user = await get_user(user_id)
    if not user:
        await message.answer("Сначала запусти программу командой /start.")
        return

    tasks = await get_user_tasks(user_id)
    task_keys = [t["task_key"] for t in tasks]
    progress = await compute_progress(user_id, user["start_date"], task_keys)
    lines = [
        f"*Прогресс: неделя {progress['current_week']} из {progress['total_weeks']}* "
        f"(день {progress['days_elapsed']} из {progress['total_days']})\n",
        f"Дней с отметками: {progress['days_with_data']}",
        f"Идеальных дней: {progress['perfect_days']}\n",
        "*По задачам (% выполнения):*",
    ]
    for t in tasks:
        lines.append(f"{t['emoji']} {t['title']}: {progress['per_task_pct'].get(t['task_key'], 0)}%")
    if progress["finished"]:
        lines.append("\n🎉 Программа на 6 месяцев завершена — поздравляю!")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    pending = pending_checkins.get(user_id)
    if not pending or callback.message.message_id != pending["message_id"]:
        await callback.answer("Этот чек-ин устарел, вызови /today ещё раз.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    pending["states"][key] = not pending["states"].get(key, False)
    tasks = await get_user_tasks(user_id)
    await callback.message.edit_reply_markup(reply_markup=build_checkin_keyboard(tasks, pending["states"]))
    await callback.answer()


@dp.callback_query(F.data == "confirm")
async def cb_confirm(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    pending = pending_checkins.get(user_id)
    if not pending or callback.message.message_id != pending["message_id"]:
        await callback.answer("Этот чек-ин устарел, вызови /today ещё раз.", show_alert=True)
        return
    await save_checkin(user_id, pending["date"], pending["states"])
    done_count = sum(pending["states"].values())
    total = len(pending["states"])
    await callback.message.edit_text(
        f"Сохранено ✅ Выполнено {done_count} из {total} за {pending['date']}.\n"
        "Посмотреть прогресс — /progress"
    )
    del pending_checkins[user_id]
    await callback.answer("Сохранено!")


# ---------------------------------------------------------------------------
# Планировщик напоминаний
# ---------------------------------------------------------------------------
def schedule_reminder(user_id: int, hour: int, minute: int) -> None:
    scheduler.add_job(
        send_checkin,
        trigger=CronTrigger(hour=hour, minute=minute),
        args=[user_id],
        id=f"reminder_{user_id}",
        replace_existing=True,
    )


async def restore_schedules() -> None:
    for user in await get_all_users():
        schedule_reminder(user["user_id"], user["reminder_hour"], user["reminder_minute"])


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------
async def main() -> None:
    await init_db()
    await restore_schedules()
    scheduler.start()

    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)

    await asyncio.gather(
        server.serve(),
        dp.start_polling(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
