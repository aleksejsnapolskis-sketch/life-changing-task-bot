"""
Life Changing Task — простой Telegram-бот (без Mini App).

Трекер 9 задач по умолчанию на 26 недель (6 месяцев), с ежедневными
напоминаниями. Задачи можно менять прямо командами в чате:

    /tasks               — список текущих задач
    /addtask Название    — добавить задачу (максимум 10)
    /addtask Название | Подсказка   — добавить с пояснением
    /removetask 3        — удалить задачу номер 3 из списка /tasks
    /renametask 3 Новое название    — переименовать задачу номер 3
    /renametask 3 Новое название | Подсказка

Остальные команды:
    /start     — регистрация, включение напоминаний
    /today     — отметиться за сегодня
    /progress  — прогресс текстом
    /settime ЧЧ:ММ — время ежедневного напоминания

Запуск:
    pip install -r requirements.txt
    set TELEGRAM_BOT_TOKEN=твой_токен          (Windows)
    python bot.py
"""

import asyncio
import logging
import os
import uuid
from datetime import date, datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DB_PATH = os.environ.get("HABITBOT_DB_PATH", "habitbot.sqlite3")
PORT = int(os.environ.get("PORT", "8000"))

TOTAL_WEEKS = 26
DEFAULT_HOUR = 21
DEFAULT_MINUTE = 0
MAX_TASKS = 10
ADMIN_TELEGRAM_ID = os.environ.get("ADMIN_TELEGRAM_ID", "")
STATS_BASE_OFFSET = 27  # прибавляется к реальному числу пользователей в /stats

DEFAULT_TASKS: list[tuple[str, str, str, str]] = [
    ("sleep", "😴", "Сон", "Отбой до 23:00 / подъём в 7:00"),
    ("sport", "🏋️", "Спорт", "Зал / бег / прогулка"),
    ("cold_shower", "🚿", "Холодный душ", "+ медитация 15 мин"),
    ("reading", "📖", "Чтение", "Книга / подкаст"),
    ("no_sugar", "☕", "Без кофе и сахара", ""),
    ("no_alco", "🍷", "Без алкоголя", "и без курения"),
    ("water", "💧", "Вода", "5 стаканов в день"),
    ("save_money", "💰", "Финансы", "Откладывать деньги / крипта"),
    ("love", "❤️", "Скажи близким", "что любишь их"),
]

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()


class TaskEdit(StatesGroup):
    waiting_add = State()
    waiting_rename = State()

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


async def add_user_task(user_id: int, title: str, hint: str) -> str | None:
    tasks = await get_user_tasks(user_id)
    if len(tasks) >= MAX_TASKS:
        return None
    task_key = uuid.uuid4().hex[:10]
    next_position = (max((t["position"] for t in tasks), default=-1)) + 1
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO user_tasks (user_id, task_key, emoji, title, hint, position)
            VALUES (?, ?, '⭐', ?, ?, ?)
            """,
            (user_id, task_key, title.strip()[:60], hint.strip()[:80], next_position),
        )
        await db.commit()
    return task_key


async def rename_user_task(user_id: int, task_key: str, title: str, hint: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE user_tasks SET title = ?, hint = ? WHERE user_id = ? AND task_key = ?",
            (title.strip()[:60], hint.strip()[:80], user_id, task_key),
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_user_task(user_id: int, task_key: str) -> bool:
    async with
