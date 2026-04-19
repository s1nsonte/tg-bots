import asyncio
import logging
import sqlite3
import os
from datetime import datetime, timedelta
from collections import defaultdict

import aiohttp
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
if not TOKEN:
    raise ValueError("BOT_TOKEN is missing or empty!")

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

DATA_DIR = "/data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "series_bot.db")

TVMAZE_SEARCH = "https://api.tvmaze.com/search/shows?q="
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")


def get_db():
    return sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)


def init_db():
    with get_db() as conn:
        cur = conn.cursor()
        cur.executescript('''
            CREATE TABLE IF NOT EXISTS users (...);           -- оставь как было
            CREATE TABLE IF NOT EXISTS series (...);          -- оставь как было
            CREATE TABLE IF NOT EXISTS watched_episodes (...);
            CREATE TABLE IF NOT EXISTS finished_seasons (...);
        ''')
    print("✅ База данных инициализирована")


# ====================== КЛАВИАТУРЫ ======================
def series_keyboard(series_id: int, completed: bool = False, notifications: bool = True):
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


# ... (все остальные клавиатуры и TVMaze функции остаются без изменений)


# ====================== НОВЫЕ ОБРАБОТЧИКИ ======================

@dp.callback_query(F.data.startswith("complete_"))
async def complete_series(callback: types.CallbackQuery):
    series_id = int(callback.data.split("_")[-1])
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE series SET completed = 1 WHERE id = ?", (series_id,))
        conn.commit()

    await callback.answer("✅ Сериал отмечен как завершённый", show_alert=True)
    await show_series_menu(callback.message, series_id)


@dp.callback_query(F.data.startswith("delete_"))
async def delete_series(callback: types.CallbackQuery):
    series_id = int(callback.data.split("_")[-1])
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM series WHERE id = ?", (series_id,))
        cur.execute("DELETE FROM watched_episodes WHERE series_id = ?", (series_id,))
        cur.execute("DELETE FROM finished_seasons WHERE series_id = ?", (series_id,))
        conn.commit()

    await callback.answer("🗑 Сериал удалён", show_alert=True)
    await callback.message.delete()


# ====================== show_series_menu (исправленная) ======================
async def show_series_menu(message: types.Message, series_id: int):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT name, poster_file_id, airing_days, completed, notifications_enabled 
            FROM series WHERE id = ?
        """, (series_id,))
        row = cur.fetchone()
        if not row:
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

    markup = series_keyboard(series_id, completed, notif)

    try:
        await message.edit_text(caption, parse_mode="HTML", reply_markup=markup)
    except TelegramBadRequest:
        pass
    except Exception:
        if poster:
            try:
                await message.answer_photo(photo=poster, caption=caption, parse_mode="HTML", reply_markup=markup)
            except:
                await message.answer(caption, parse_mode="HTML", reply_markup=markup)
        else:
            await message.answer(caption, parse_mode="HTML", reply_markup=markup)


# ====================== ЗАПУСК ======================
@dp.startup()
async def on_startup():
    init_db()
    # ... (остальное как было)
    scheduler.start()
    schedule_notifications()
    print("🤖 Бот запущен с исправленными кнопками")


async def main():
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
