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


# ====================== БАЗА ДАННЫХ ======================
def get_db():
    return sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)


def init_db():
    with get_db() as conn:
        cur = conn.cursor()
        
        # Создаём таблицы
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
                tvmaze_id INTEGER,
                poster_file_id TEXT,
                airing_days TEXT,
                episodes_per_season INTEGER DEFAULT 24,
                completed BOOLEAN DEFAULT FALSE,
                notifications_enabled BOOLEAN DEFAULT TRUE
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

        # Автоматическое добавление недостающих колонок (критично для старых БД)
        try:
            cur.execute("ALTER TABLE series ADD COLUMN notifications_enabled BOOLEAN DEFAULT TRUE")
            print("✅ Добавлена колонка notifications_enabled")
        except sqlite3.OperationalError:
            pass  # колонка уже существует

        conn.commit()
    print("✅ База данных инициализирована и обновлена")


# ====================== КЛАВИАТУРЫ ======================
def skip_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="skip_original")]])


def confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, добавить", callback_data="confirm_add")],
        [InlineKeyboardButton(text="❌ Нет, отменить", callback_data="cancel_add")]
    ])


def notification_time_keyboard():
    hours = [InlineKeyboardButton(text=f"{h:02d}:00", callback_data=f"set_time_{h}_0") for h in range(24)]
    keyboard = [hours[i:i + 4] for i in range(0, len(hours), 4)]
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


# ====================== TVMAZE + ПОСТЕР ======================
async def search_tvmaze(query: str):
    if not query: return None
    async with aiohttp.ClientSession() as session:
        async with session.get(TVMAZE_SEARCH + query.replace(" ", "+")) as resp:
            if resp.status != 200: return None
            data = await resp.json()
            return data[0]['show'] if data else None


async def get_next_episode(tvmaze_id: int):
    if not tvmaze_id: return None
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.tvmaze.com/shows/{tvmaze_id}/nextepisode") as resp:
            if resp.status in (404, 204): return None
            if resp.status != 200: return None
            return await resp.json()


async def get_episodes_per_season(tvmaze_id: int) -> int:
    if not tvmaze_id: return 24
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.tvmaze.com/shows/{tvmaze_id}/episodes") as resp:
            if resp.status != 200: return 24
            try:
                episodes = await resp.json()
            except:
                return 24
            if not episodes: return 24

            season_ep_count = defaultdict(int)
            for ep in episodes:
                if ep.get('season') is not None:
                    season_ep_count[ep['season']] += 1
            return max(season_ep_count.values()) if season_ep_count else 24


async def download_poster_silently(tvmaze_show: dict, user_id: int) -> str | None:
    image = tvmaze_show.get('image')
    if not image or not image.get('original'):
        return None
    try:
        msg = await bot.send_photo(chat_id=user_id, photo=image['original'], disable_notification=True)
        file_id = msg.photo[-1].file_id
        await msg.delete()
        return file_id
    except Exception as e:
        logging.error(f"Постер не загружен: {e}")
        return None


# ====================== ВСПОМОГАТЕЛЬНЫЕ ======================
def get_watched_count(series_id: int) -> int:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM watched_episodes WHERE series_id = ?", (series_id,))
        return cur.fetchone()[0]


def get_active_seasons_progress(series_id: int):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT episodes_per_season FROM series WHERE id = ?", (series_id,))
        eps = cur.fetchone()[0] or 24

        cur.execute("SELECT season, COUNT(episode) FROM watched_episodes WHERE series_id = ? GROUP BY season", (series_id,))
        watched_dict = dict(cur.fetchall())

        cur.execute("SELECT season FROM finished_seasons WHERE series_id = ?", (series_id,))
        finished = {row[0] for row in cur.fetchall()}

        seasons = sorted(set(watched_dict.keys()) | finished | {1})
        return [(s, watched_dict.get(s, 0), eps, " ✅" if s in finished or watched_dict.get(s, 0) >= eps else "")
                for s in seasons]


# ====================== УВЕДОМЛЕНИЯ ======================
async def send_user_notifications(user_id: int):
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now().date() + timedelta(days=1)).strftime("%Y-%m-%d")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, name, tvmaze_id FROM series
            WHERE user_id = ? AND notifications_enabled = 1 AND completed = 0
        """, (user_id,))
        series_list = cur.fetchall()

    for series_id, name, tvmaze_id in series_list:
        if not tvmaze_id: continue
        next_ep = await get_next_episode(tvmaze_id)
        if not next_ep: continue

        air_date = next_ep.get('airdate')
        if air_date not in (today, tomorrow): continue

        season = next_ep.get('season')
        episode = next_ep.get('number')
        summary = next_ep.get('summary', '')[:250].replace('<p>', '').replace('</p>', '')

        text = f"🔔 **Новая серия скоро!**\n\n🎬 {name}\nS{season}E{episode} — {air_date}\n\n{summary}"
        try:
            await bot.send_message(user_id, text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Уведомление не отправлено {user_id}: {e}")


def schedule_notifications():
    scheduler.remove_all_jobs()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, notification_hour, notification_minute FROM users")
        for user_id, hour, minute in cur.fetchall():
            scheduler.add_job(send_user_notifications, CronTrigger(hour=hour, minute=minute),
                              args=(user_id,), id=f"notif_{user_id}", replace_existing=True)
    print("✅ Планировщик уведомлений обновлён")


# ====================== FSM ======================
class BotStates(StatesGroup):
    add_name = State()
    add_original_name = State()
    add_confirm_tvmaze = State()
    select_season_mark = State()
    select_season_finish = State()
    mark_multiple_episodes = State()


# ====================== ОСНОВНЫЕ ХЕНДЛЕРЫ ======================
# ... (cmd_add, process_add_name, try_search_tvmaze, confirm_add и т.д. — они такие же, как в предыдущей версии)

# Для краткости я опущу повторяющиеся части (add, mark_episodes, toggle, calendar и т.д.).
# Ниже только критически важные части + исправленный cmd_my и show_series_menu.

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

        markup = series_keyboard(sid, completed, notif)

        if poster:
            try:
                await message.answer_photo(photo=poster, caption=caption, parse_mode="HTML", reply_markup=markup)
            except:
                await message.answer(caption, parse_mode="HTML", reply_markup=markup)
        else:
            await message.answer(caption, parse_mode="HTML", reply_markup=markup)


async def show_series_menu(message: types.Message, series_id: int):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT name, poster_file_id, airing_days, completed, notifications_enabled 
            FROM series WHERE id = ?
        """, (series_id,))
        row = cur.fetchone()
        if not row: return
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
    init_db()   # ← Здесь происходит обновление колонок

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT user_id FROM series")
        for (uid,) in cur.fetchall():
            cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
        conn.commit()

    scheduler.start()
    schedule_notifications()
    print("🤖 Бот запущен с обновлённой базой данных!")


@dp.shutdown()
async def on_shutdown():
    scheduler.shutdown(wait=False)
    await bot.session.close()


async def main():
    print("🚀 Запуск бота...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
