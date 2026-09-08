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
import random
import uuid
from datetime import date, datetime

import aiosqlite
from aiohttp import web
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
MAX_TASKS = 12
ADMIN_TELEGRAM_ID = os.environ.get("ADMIN_TELEGRAM_ID", "")
STATS_BASE_OFFSET = 27  # прибавляется к реальному числу пользователей в /stats
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "")  # например: mychannel (без @)

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

MOTIVATIONAL_MESSAGES: list[str] = [
    "Ты большой, ты молодец, у тебя всё получится.",
    "Я в тебя верю, поверь и ты в себя.",
    "Я благодарен этому миру за всё то, что у меня есть.",
    "Ты справляешься лучше, чем думаешь — гордись собой.",
    "Каждый новый день — это шанс стать чуть лучше, чем вчера.",
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
                reminder_minute INTEGER NOT NULL DEFAULT 0,
                bedtime_hour INTEGER NOT NULL DEFAULT 23,
                bedtime_minute INTEGER NOT NULL DEFAULT 0,
                wake_hour INTEGER NOT NULL DEFAULT 7,
                wake_minute INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        for column, default in (
            ("bedtime_hour", 23),
            ("bedtime_minute", 0),
            ("wake_hour", 7),
            ("wake_minute", 0),
        ):
            try:
                await db.execute(
                    f"ALTER TABLE users ADD COLUMN {column} INTEGER NOT NULL DEFAULT {default}"
                )
                await db.commit()
            except Exception:
                pass  # колонка уже есть — это нормально
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_tasks (
                user_id INTEGER NOT NULL,
                task_key TEXT NOT NULL,
                emoji TEXT NOT NULL DEFAULT '⭐',
                title TEXT NOT NULL,
                hint TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL,
                notify INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (user_id, task_key)
            )
            """
        )
        try:
            await db.execute("ALTER TABLE user_tasks ADD COLUMN notify INTEGER NOT NULL DEFAULT 1")
            await db.commit()
        except Exception:
            pass  # колонка уже есть — это нормально
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


async def set_bedtime(user_id: int, hour: int, minute: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET bedtime_hour = ?, bedtime_minute = ? WHERE user_id = ?",
            (hour, minute, user_id),
        )
        await db.commit()


async def set_wake_time(user_id: int, hour: int, minute: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET wake_hour = ?, wake_minute = ? WHERE user_id = ?",
            (hour, minute, user_id),
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
    schedule_bedtime(user_id, 23, 0)
    schedule_wake(user_id, 7, 0)
    return await get_user(user_id)


async def get_user_tasks(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT task_key, emoji, title, hint, position, notify FROM user_tasks "
            "WHERE user_id = ? ORDER BY position",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def toggle_task_notify(user_id: int, task_key: str) -> bool | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT notify FROM user_tasks WHERE user_id = ? AND task_key = ?",
            (user_id, task_key),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        new_value = 0 if row[0] else 1
        await db.execute(
            "UPDATE user_tasks SET notify = ? WHERE user_id = ? AND task_key = ?",
            (new_value, user_id, task_key),
        )
        await db.commit()
        return bool(new_value)


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


async def count_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_all_checkins(user_id: int) -> list[tuple[str, str, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT day, task_key, done FROM checkins WHERE user_id = ? ORDER BY day",
            (user_id,),
        ) as cur:
            return await cur.fetchall()


# ---------------------------------------------------------------------------
# Прогресс
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

    for day_str, task_key, done in rows:
        if task_key not in per_task_done:
            continue
        days_with_data.add(day_str)
        if done:
            per_task_done[task_key] += 1
            day_totals[day_str] = day_totals.get(day_str, 0) + 1

    perfect_days = sum(1 for d in days_with_data if day_totals.get(d, 0) == len(task_keys))
    denom = max(len(days_with_data), 1)
    per_task_pct = {key: round(100 * per_task_done.get(key, 0) / denom) for key in task_keys}

    return {
        "days_elapsed": min(days_elapsed, total_days_planned),
        "total_days": total_days_planned,
        "current_week": current_week,
        "total_weeks": TOTAL_WEEKS,
        "days_with_data": len(days_with_data),
        "perfect_days": perfect_days,
        "per_task_pct": per_task_pct,
        "finished": days_elapsed > total_days_planned,
    }


def tasks_description(tasks: list[dict]) -> str:
    if not tasks:
        return "Список задач пуст. Добавь через /addtask Название"
    lines = ["*Твои задачи:*\n"]
    for i, t in enumerate(tasks, start=1):
        hint_part = f" — {t['hint']}" if t["hint"] else ""
        lines.append(f"{i}. {t['emoji']} *{t['title']}*{hint_part}")
    return "\n".join(lines)


def parse_title_hint(text: str) -> tuple[str, str]:
    if "|" in text:
        title, hint = text.split("|", 1)
        return title.strip(), hint.strip()
    return text.strip(), ""


# ---------------------------------------------------------------------------
# Клавиатура чек-ина
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


async def send_checkin(user_id: int, automatic: bool = False) -> None:
    all_tasks = await get_user_tasks(user_id)
    if not all_tasks:
        try:
            await bot.send_message(user_id, "У тебя пока нет задач. Добавь через /addtask Название")
        except Exception:
            logging.exception("Не удалось отправить сообщение пользователю %s", user_id)
        return

    # Для автоматического (по расписанию) напоминания показываем только
    # задачи, для которых уведомления не отключены. Для ручного /today —
    # всегда полный список, чтобы отметить можно было любую задачу.
    tasks = [t for t in all_tasks if t.get("notify", 1)] if automatic else all_tasks
    if automatic and not tasks:
        return  # у пользователя все уведомления отключены — тихо пропускаем

    today_str = date.today().isoformat()
    existing = await get_existing_checkin(user_id, today_str)
    states = {t["task_key"]: existing.get(t["task_key"], False) for t in tasks}

    text = f"Как прошёл день ({today_str})? Отметь, что выполнил:"
    if automatic:
        text = random.choice(MOTIVATIONAL_MESSAGES) + "\n\n" + text

    try:
        msg = await bot.send_message(
            user_id,
            text,
            reply_markup=build_checkin_keyboard(tasks, states),
        )
    except Exception:
        logging.exception("Не удалось отправить чек-ин пользователю %s", user_id)
        return
    pending_checkins[user_id] = {"date": today_str, "states": states, "message_id": msg.message_id}


# ---------------------------------------------------------------------------
# Проверка подписки на канал (перед тем как открыть бота)
# ---------------------------------------------------------------------------
async def is_subscribed(user_id: int) -> bool:
    if not REQUIRED_CHANNEL:
        return True  # проверка выключена, если канал не задан
    try:
        member = await bot.get_chat_member(chat_id=f"@{REQUIRED_CHANNEL}", user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        logging.exception("Не удалось проверить подписку для %s", user_id)
        return False


def subscribe_gate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Открыть канал", url=f"https://t.me/{REQUIRED_CHANNEL}")],
            [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")],
        ]
    )


async def send_start_content(message_or_callback, user_id: int) -> None:
    """Отправляет обычное содержимое /start (после прохождения проверки подписки)."""
    existing = await get_user(user_id)
    if existing:
        total = await count_users() + STATS_BASE_OFFSET
        tasks = await get_user_tasks(user_id)
        await message_or_callback.answer(
            f"Ты уже участвуешь в программе! (👥 всего присоединилось: {total})\n\n"
            + tasks_description(tasks)
            + "\n\nКоманды:\n"
            "/today — отметиться сегодня\n"
            "/progress — прогресс\n"
            "/edittasks — изменить задачи (кнопками, легко)\n"
            "/settime ЧЧ:ММ — время напоминания\n"
            "/setbedtime ЧЧ:ММ — время напоминания об отбое\n"
            "/setwake ЧЧ:ММ — время напоминания о подъёме",
            parse_mode="Markdown",
        )
        return

    await ensure_user(user_id)
    tasks = await get_user_tasks(user_id)
    total = await count_users() + STATS_BASE_OFFSET
    await message_or_callback.answer(
        "Добро пожаловать в *Life Changing Task*!\n\n"
        f"👥 К программе уже присоединилось: *{total}* человек\n\n"
        f"Программа на *26 недель (6 месяцев)*. Каждый день в "
        f"{DEFAULT_HOUR:02d}:{DEFAULT_MINUTE:02d} буду присылать напоминание.\n\n"
        + tasks_description(tasks)
        + "\n\nЗадачи можно менять — открой /edittasks и жми кнопки "
        "(✏️ переименовать, ❌ удалить, ➕ добавить).\n\n"
        "Начать сегодня — /today",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------
@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id
    if not await is_subscribed(user_id):
        await message.answer(
            "Чтобы открыть трекер, сначала подпишись на канал 👇",
            reply_markup=subscribe_gate_keyboard(),
        )
        return
    await send_start_content(message, user_id)


@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if not await is_subscribed(user_id):
        await callback.answer("Пока не вижу подписку — попробуй ещё раз через пару секунд.", show_alert=True)
        return
    await callback.answer("Подписка подтверждена! 🎉")
    await callback.message.delete()
    await send_start_content(callback.message, user_id)


@dp.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    await message.answer(f"Твой Telegram ID: `{message.from_user.id}`", parse_mode="Markdown")


@dp.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if ADMIN_TELEGRAM_ID and str(message.from_user.id) != ADMIN_TELEGRAM_ID:
        return  # тихо игнорируем, если задан админ и это не он
    total = await count_users() + STATS_BASE_OFFSET
    await message.answer(f"Всего подключилось к программе: *{total}* человек", parse_mode="Markdown")


@dp.message(Command("tasks"))
async def cmd_tasks(message: Message) -> None:
    await ensure_user(message.from_user.id)
    tasks = await get_user_tasks(message.from_user.id)
    await message.answer(tasks_description(tasks), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Удобное редактирование задач кнопками (/edittasks)
# ---------------------------------------------------------------------------
def build_edit_keyboard(tasks: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for t in tasks:
        bell = "🔔" if t.get("notify", 1) else "🔕"
        rows.append(
            [
                InlineKeyboardButton(text=f"{t['emoji']} {t['title']}", callback_data="noop"),
                InlineKeyboardButton(text=bell, callback_data=f"notify:{t['task_key']}"),
                InlineKeyboardButton(text="✏️", callback_data=f"edit:{t['task_key']}"),
                InlineKeyboardButton(text="❌", callback_data=f"del:{t['task_key']}"),
            ]
        )
    if len(tasks) < MAX_TASKS:
        rows.append([InlineKeyboardButton(text="➕ Добавить задачу", callback_data="addnew")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("edittasks"))
async def cmd_edittasks(message: Message, state: FSMContext) -> None:
    await state.clear()
    await ensure_user(message.from_user.id)
    tasks = await get_user_tasks(message.from_user.id)
    if not tasks:
        await message.answer(
            "У тебя пока нет задач.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить задачу", callback_data="addnew")]]
            ),
        )
        return
    await message.answer(
        "*Твои задачи* — жми 🔔 чтобы вкл/выкл напоминания, ✏️ переименовать, ❌ удалить:",
        parse_mode="Markdown",
        reply_markup=build_edit_keyboard(tasks),
    )


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@dp.callback_query(F.data == "addnew")
async def cb_addnew(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TaskEdit.waiting_add)
    await callback.message.answer(
        "Напиши название новой задачи (можно с подсказкой через |, например:\n"
        "Бег | 20 минут утром)"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("edit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext) -> None:
    task_key = callback.data.split(":", 1)[1]
    await state.set_state(TaskEdit.waiting_rename)
    await state.update_data(task_key=task_key)
    await callback.message.answer(
        "Напиши новое название для этой задачи (можно с подсказкой через |, например:\n"
        "Йога | 15 минут вечером)"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("notify:"))
async def cb_toggle_notify(callback: CallbackQuery) -> None:
    task_key = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    new_state = await toggle_task_notify(user_id, task_key)
    tasks = await get_user_tasks(user_id)
    await callback.message.edit_reply_markup(reply_markup=build_edit_keyboard(tasks))
    if new_state:
        await callback.answer("🔔 Напоминания включены для этой задачи")
    else:
        await callback.answer("🔕 Напоминания отключены для этой задачи")


@dp.callback_query(F.data.startswith("del:"))
async def cb_delete(callback: CallbackQuery) -> None:
    task_key = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    await delete_user_task(user_id, task_key)
    tasks = await get_user_tasks(user_id)
    if not tasks:
        await callback.message.edit_text(
            "Список задач пуст.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить задачу", callback_data="addnew")]]
            ),
        )
    else:
        await callback.message.edit_text(
            "*Твои задачи* — жми 🔔 чтобы вкл/выкл напоминания, ✏️ переименовать, ❌ удалить:",
            parse_mode="Markdown",
            reply_markup=build_edit_keyboard(tasks),
        )
    await callback.answer("Удалено")


@dp.message(TaskEdit.waiting_add, F.text)
async def process_add_task(message: Message, state: FSMContext) -> None:
    await state.clear()
    title, hint = parse_title_hint(message.text)
    if not title:
        await message.answer("Название не может быть пустым. Открой /edittasks и попробуй снова.")
        return
    task_key = await add_user_task(message.from_user.id, title, hint)
    if not task_key:
        await message.answer(f"Уже максимум задач ({MAX_TASKS}) — сначала удали одну.")
        return
    tasks = await get_user_tasks(message.from_user.id)
    await message.answer(
        "Добавлено! *Твои задачи:*",
        parse_mode="Markdown",
        reply_markup=build_edit_keyboard(tasks),
    )


@dp.message(TaskEdit.waiting_rename, F.text)
async def process_rename_task(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    task_key = data.get("task_key")
    await state.clear()
    title, hint = parse_title_hint(message.text)
    if not title:
        await message.answer("Название не может быть пустым. Открой /edittasks и попробуй снова.")
        return
    await rename_user_task(message.from_user.id, task_key, title, hint)
    tasks = await get_user_tasks(message.from_user.id)
    await message.answer(
        "Переименовано! *Твои задачи:*",
        parse_mode="Markdown",
        reply_markup=build_edit_keyboard(tasks),
    )


@dp.message(Command("addtask"))
async def cmd_addtask(message: Message) -> None:
    await ensure_user(message.from_user.id)
    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.answer("Формат: /addtask Название задачи\nили: /addtask Название | Подсказка")
        return
    title, hint = parse_title_hint(text)
    if not title:
        await message.answer("Название задачи не может быть пустым.")
        return
    task_key = await add_user_task(message.from_user.id, title, hint)
    if not task_key:
        await message.answer(f"Уже максимум задач ({MAX_TASKS}) — сначала удали одну через /removetask N")
        return
    tasks = await get_user_tasks(message.from_user.id)
    await message.answer("Добавлено!\n\n" + tasks_description(tasks), parse_mode="Markdown")


@dp.message(Command("removetask"))
async def cmd_removetask(message: Message) -> None:
    await ensure_user(message.from_user.id)
    arg = message.text.partition(" ")[2].strip()
    tasks = await get_user_tasks(message.from_user.id)
    if not arg.isdigit() or not (1 <= int(arg) <= len(tasks)):
        await message.answer(f"Формат: /removetask N, где N — номер из списка /tasks (1-{len(tasks)})")
        return
    index = int(arg) - 1
    task = tasks[index]
    await delete_user_task(message.from_user.id, task["task_key"])
    tasks = await get_user_tasks(message.from_user.id)
    await message.answer(f"Удалено: {task['emoji']} {task['title']}\n\n" + tasks_description(tasks), parse_mode="Markdown")


@dp.message(Command("renametask"))
async def cmd_renametask(message: Message) -> None:
    await ensure_user(message.from_user.id)
    rest = message.text.partition(" ")[2].strip()
    parts = rest.split(" ", 1)
    tasks = await get_user_tasks(message.from_user.id)
    if len(parts) < 2 or not parts[0].isdigit() or not (1 <= int(parts[0]) <= len(tasks)):
        await message.answer(
            f"Формат: /renametask N Новое название (N — номер из списка /tasks, 1-{len(tasks)})\n"
            "Можно добавить подсказку через |: /renametask 2 Бег | 20 минут утром"
        )
        return
    index = int(parts[0]) - 1
    title, hint = parse_title_hint(parts[1])
    if not title:
        await message.answer("Название задачи не может быть пустым.")
        return
    task = tasks[index]
    await rename_user_task(message.from_user.id, task["task_key"], title, hint)
    tasks = await get_user_tasks(message.from_user.id)
    await message.answer("Переименовано!\n\n" + tasks_description(tasks), parse_mode="Markdown")


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


def _parse_time_arg(message: Message) -> tuple[int, int] | None:
    parts = message.text.strip().split()
    if len(parts) != 2 or ":" not in parts[1]:
        return None
    try:
        hour_str, minute_str = parts[1].split(":")
        hour, minute = int(hour_str), int(minute_str)
        assert 0 <= hour <= 23 and 0 <= minute <= 59
    except (ValueError, AssertionError):
        return None
    return hour, minute


@dp.message(Command("setbedtime"))
async def cmd_setbedtime(message: Message) -> None:
    user_id = message.from_user.id
    if not await get_user(user_id):
        await message.answer("Сначала запусти программу командой /start.")
        return
    parsed = _parse_time_arg(message)
    if not parsed:
        await message.answer("Формат: /setbedtime 23:00")
        return
    hour, minute = parsed
    await set_bedtime(user_id, hour, minute)
    schedule_bedtime(user_id, hour, minute)
    await message.answer(f"Готово! Буду напоминать про отбой в {hour:02d}:{minute:02d}.")


@dp.message(Command("setwake"))
async def cmd_setwake(message: Message) -> None:
    user_id = message.from_user.id
    if not await get_user(user_id):
        await message.answer("Сначала запусти программу командой /start.")
        return
    parsed = _parse_time_arg(message)
    if not parsed:
        await message.answer("Формат: /setwake 7:00")
        return
    hour, minute = parsed
    await set_wake_time(user_id, hour, minute)
    schedule_wake(user_id, hour, minute)
    await message.answer(f"Готово! Буду напоминать про подъём в {hour:02d}:{minute:02d}.")


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
# Планировщик
# ---------------------------------------------------------------------------
def schedule_reminder(user_id: int, hour: int, minute: int) -> None:
    scheduler.add_job(
        send_checkin,
        trigger=CronTrigger(hour=hour, minute=minute),
        args=[user_id, True],
        id=f"reminder_{user_id}",
        replace_existing=True,
    )


async def restore_schedules() -> None:
    for user in await get_all_users():
        schedule_reminder(user["user_id"], user["reminder_hour"], user["reminder_minute"])
        schedule_bedtime(user["user_id"], user["bedtime_hour"], user["bedtime_minute"])
        schedule_wake(user["user_id"], user["wake_hour"], user["wake_minute"])


# ---------------------------------------------------------------------------
# Персональные напоминания про сон — у каждого своё время отбоя и подъёма
# (по умолчанию 23:00 / 7:00, можно поменять командами /setbedtime и /setwake).
# ---------------------------------------------------------------------------
async def send_bedtime_reminder(user_id: int) -> None:
    try:
        await bot.send_message(user_id, "😴 Пора ложиться спать. Отбой!")
    except Exception:
        logging.exception("Не удалось отправить напоминание об отбое пользователю %s", user_id)


async def send_wake_reminder(user_id: int) -> None:
    try:
        await bot.send_message(user_id, "☀️ Доброе утро, пора вставать!")
    except Exception:
        logging.exception("Не удалось отправить напоминание о подъёме пользователю %s", user_id)


def schedule_bedtime(user_id: int, hour: int, minute: int) -> None:
    scheduler.add_job(
        send_bedtime_reminder,
        trigger=CronTrigger(hour=hour, minute=minute),
        args=[user_id],
        id=f"bedtime_{user_id}",
        replace_existing=True,
    )


def schedule_wake(user_id: int, hour: int, minute: int) -> None:
    scheduler.add_job(
        send_wake_reminder,
        trigger=CronTrigger(hour=hour, minute=minute),
        args=[user_id],
        id=f"wake_{user_id}",
        replace_existing=True,
    )


# ---------------------------------------------------------------------------
# Мини-веб-сервер для Render (просто отвечает "OK", чтобы платформа
# не решила, что сервис завис — без этого хостинг перезапускает бота).
# Никакого отношения к функциям бота не имеет. Используем aiohttp —
# надёжную готовую библиотеку, а не самописный обработчик, чтобы правильно
# отвечать на технические запросы Render и внешнего пинг-сервиса.
# ---------------------------------------------------------------------------
async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def run_health_server() -> None:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/{tail:.*}", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    while True:
        await asyncio.sleep(3600)


async def main() -> None:
    await init_db()
    await restore_schedules()
    scheduler.start()
    await asyncio.gather(
        run_health_server(),
        dp.start_polling(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
