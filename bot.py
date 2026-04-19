import asyncio
import logging
import sqlite3
import os
from datetime import datetime, timedelta
from collections import defaultdict
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
KINOPOISK_API_KEY = os.getenv("KINOPOISK_API_KEY")

if not TOKEN:
    raise ValueError("BOT_TOKEN is missing or empty!")

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

DATA_DIR = "/data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "series_bot.db")

# ====================== API URLs ======================
TMDB_SEARCH = "https://api.themoviedb.org/3/search/tv"
TMDB_DETAILS = "https://api.themoviedb.org/3/tv/{tmdb_id}"
KINOPOISK_SEARCH = "https://kinopoiskapiunofficial.tech/api/v2.2/films/search-by-keyword"
KINOPOISK_DETAILS = "https://kinopoiskapiunofficial.tech/api/v2.2/films/{kp_id}"
TVMAZE_SEARCH = "https://api.tvmaze.com/search/shows?q="

scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

# ====================== БАЗА ДАННЫХ ======================
def get_db():
    return sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)

def init_db():
    with get_db() as conn:
        cur = conn.cursor()
        cur.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                notification_hour INTEGER DEFAULT 10,
                notification_minute INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS series (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                original_name TEXT,
                tmdb_id INTEGER,
                kinopoisk_id INTEGER,
                tvmaze_id INTEGER,
                poster_file_id TEXT,
                airing_days TEXT,
                episodes_per_season INTEGER DEFAULT 24,
                completed BOOLEAN DEFAULT FALSE,
                notifications_enabled BOOLEAN DEFAULT TRUE,
                start_season INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS watched_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER,
                season INTEGER,
                episode INTEGER,
                UNIQUE(series_id, season, episode)
            );
            CREATE TABLE IF NOT EXISTS finished_seasons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER,
                season INTEGER,
                UNIQUE(series_id, season)
            );
        ''')

        columns = [
            ("original_name", "TEXT"), ("tmdb_id", "INTEGER"), ("kinopoisk_id", "INTEGER"),
            ("tvmaze_id", "INTEGER"), ("poster_file_id", "TEXT"), ("airing_days", "TEXT"),
            ("notifications_enabled", "BOOLEAN DEFAULT TRUE"), ("start_season", "INTEGER DEFAULT 1")
        ]
        for col, col_type in columns:
            try:
                cur.execute(f"ALTER TABLE series ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass
        conn.commit()
    print("✅ База данных инициализирована")

# ====================== КЛАВИАТУРЫ ======================
def skip_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="skip_original")]])

def notification_time_keyboard():
    hours = [InlineKeyboardButton(text=f"{h:02d}:00", callback_data=f"set_time_{h}_0") for h in range(24)]
    keyboard = [hours[i:i+4] for i in range(0, len(hours), 4)]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def episodes_keyboard(series_id: int, season: int, total: int):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT episode FROM watched_episodes WHERE series_id = ? AND season = ?", (series_id, season))
        watched = {row[0] for row in cur.fetchall()}
    buttons = []
    row = []
    for ep in range(1, total + 1):
        status = "✅" if ep in watched else "⬜"
        row.append(InlineKeyboardButton(text=f"{status} {ep}", callback_data=f"toggle_ep_{series_id}_{season}_{ep}"))
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="✅ Готово", callback_data=f"finish_marking_{series_id}")])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_marking")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Новая компактная клавиатура для /my
def compact_series_keyboard(series_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit_series_{series_id}")]
    ])

# Полное меню при нажатии "Изменить"
def full_series_keyboard(series_id: int, completed: bool = False, notifications: bool = True):
    keyboard = [
        [InlineKeyboardButton(text="📺 Отметить просмотренные эпизоды", callback_data=f"mark_episodes_{series_id}")],
        [InlineKeyboardButton(text="🏁 Завершить сезон", callback_data=f"finish_season_{series_id}")],
        [InlineKeyboardButton(text="📅 Календарь выхода", callback_data=f"calendar_{series_id}")],
    ]
    notif_text = "🔔 Выключить уведомления" if notifications else "🔕 Включить уведомления"
    keyboard.append([InlineKeyboardButton(text=notif_text, callback_data=f"toggle_notif_{series_id}")])
    keyboard.append([InlineKeyboardButton(text="⏰ Изменить время уведомлений", callback_data="change_time")])
    if not completed:
        keyboard.append([InlineKeyboardButton(text="🛑 Завершить сериал", callback_data=f"complete_{series_id}")])
    keyboard.append([InlineKeyboardButton(text="🗑 Удалить сериал", callback_data=f"delete_{series_id}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ====================== API ФУНКЦИИ (без изменений) ======================
# ... (search_tmdb, search_kinopoisk, search_tvmaze, get_next_episode и т.д. остаются как в предыдущей версии)

# Для экономии места я опустил повторяющиеся функции. 
# Они такие же, как в последней версии, которую я присылал.

# ====================== /my — компактный вид ======================
@dp.message(Command("my"))
async def cmd_my(message: types.Message):
    user_id = message.from_user.id
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, name, poster_file_id, airing_days, completed, notifications_enabled
            FROM series WHERE user_id = ?
        """, (user_id,))
        series_list = cur.fetchall()

    if not series_list:
        await message.answer("У тебя пока нет сериалов.")
        return

    days_map = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    for sid, name, poster, airing_days, completed, notif in series_list:
        airing_str = ', '.join(days_map[int(d)] for d in airing_days.split(',')) if airing_days else "—"
        total_watched = get_watched_count(sid)
        progress_lines = [f"Сезон {s}: {w}/{t}{st}" for s, w, t, st in get_active_seasons_progress(sid)]

        caption = (f"🎬 <b>{name}</b>\n"
                   f"📅 Выход: {airing_str}\n"
                   f"👁 Просмотрено всего: {total_watched}\n\n"
                   + "\n".join(progress_lines))

        markup = compact_series_keyboard(sid)

        if poster:
            try:
                await message.answer_photo(photo=poster, caption=caption, parse_mode="HTML", reply_markup=markup)
            except:
                await message.answer(caption, parse_mode="HTML", reply_markup=markup)
        else:
            await message.answer(caption, parse_mode="HTML", reply_markup=markup)

# ====================== Открытие полного меню ======================
@dp.callback_query(F.data.startswith("edit_series_"))
async def edit_series(callback: types.CallbackQuery):
    series_id = int(callback.data.split("_")[-1])
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT name, poster_file_id, airing_days, completed, notifications_enabled
            FROM series WHERE id = ?
        """, (series_id,))
        row = cur.fetchone()
        if not row:
            await callback.answer("Сериал не найден", show_alert=True)
            return
        name, poster, airing_days, completed, notif = row

    days_map = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    airing_str = ', '.join(days_map[int(d)] for d in airing_days.split(',')) if airing_days else "—"
    total_watched = get_watched_count(series_id)
    progress_lines = [f"Сезон {s}: {w}/{t}{st}" for s, w, t, st in get_active_seasons_progress(series_id)]

    caption = (f"🎬 <b>{name}</b>\n"
               f"📅 Выход: {airing_str}\n"
               f"👁 Просмотрено всего: {total_watched}\n\n"
               + "\n".join(progress_lines))

    markup = full_series_keyboard(series_id, completed, notif)

    try:
        await callback.message.edit_caption(caption, parse_mode="HTML", reply_markup=markup)
    except TelegramBadRequest:
        if poster:
            await callback.message.answer_photo(photo=poster, caption=caption, parse_mode="HTML", reply_markup=markup)
        else:
            await callback.message.answer(caption, parse_mode="HTML", reply_markup=markup)

    await callback.answer()

# ====================== Остальные хендлеры (mark_episodes, toggle_episode и т.д.) ======================
# Они остаются без изменений. Если нужно — могу добавить их полностью.

# ====================== ЗАПУСК ======================
async def main():
    init_db()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT user_id FROM series")
        for (uid,) in cur.fetchall():
            cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
        conn.commit()

    scheduler.start()
    schedule_notifications()

    print("🚀 Бот запущен — компактное меню в /my")
    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    )

if __name__ == "__main__":
    asyncio.run(main())
