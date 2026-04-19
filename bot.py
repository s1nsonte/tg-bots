import asyncio
import logging
import sqlite3
import os
import aiohttp
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
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
TVMAZE_SEASONS = "https://api.tvmaze.com/shows/{}/seasons"

scheduler = AsyncIOScheduler()

class BotStates(StatesGroup):
    add_name = State()
    add_original_name = State()
    add_confirm_tvmaze = State()
    select_season_mark = State()
    select_season_finish = State()
    mark_multiple_episodes = State()

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
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
    print("✅ База данных инициализирована")

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
    keyboard = [hours[i:i+4] for i in range(0, len(hours), 4)]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def episodes_keyboard(series_id: int, season: int, total: int):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT episode FROM watched_episodes WHERE series_id = ? AND season = ?", (series_id, season))
        watched = {row[0] for row in cur.fetchall()}

    buttons = []
    row = []
    for ep in range(1, total + 1):
        status = "✅" if ep in watched else "⬜"
        row.append(types.InlineKeyboardButton(text=f"{status} {ep}", callback_data=f"toggle_ep_{series_id}_{season}_{ep}"))
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([types.InlineKeyboardButton(text="✅ Готово", callback_data=f"finish_marking_{series_id}")])
    buttons.append([types.InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_marking")])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)

def series_keyboard(series_id: int, completed: bool = False, notifications: bool = True):
    keyboard = [
        [types.InlineKeyboardButton(text="📺 Отметить просмотренные эпизоды", callback_data=f"mark_episodes_{series_id}")],
        [types.InlineKeyboardButton(text="🏁 Завершить сезон", callback_data=f"finish_season_{series_id}")],
        [types.InlineKeyboardButton(text="📅 Календарь выхода", callback_data=f"calendar_{series_id}")],
    ]
    notif_text = "🔔 Выключить уведомления" if notifications else "🔕 Включить уведомления"
    keyboard.append([types.InlineKeyboardButton(text=notif_text, callback_data=f"toggle_notif_{series_id}")])
    keyboard.append([types.InlineKeyboardButton(text="⏰ Изменить время уведомлений", callback_data="change_time")])
    
    if not completed:
        keyboard.append([types.InlineKeyboardButton(text="🛑 Завершить сериал", callback_data=f"complete_{series_id}")])
    keyboard.append([types.InlineKeyboardButton(text="🗑 Удалить сериал", callback_data=f"delete_{series_id}")])
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)

# ====================== TVMAZE ======================
async def search_tvmaze(query: str):
    if not query: return None
    async with aiohttp.ClientSession() as session:
        async with session.get(TVMAZE_SEARCH + query.replace(" ", "+")) as resp:
            if resp.status != 200: return None
            data = await resp.json()
            return data[0]['show'] if data else None

async def get_seasons_episode_count(tvmaze_id: int):
    """Возвращает количество эпизодов в последнем сезоне (самое актуальное)"""
    async with aiohttp.ClientSession() as session:
        # Получаем список сезонов
        async with session.get(TVMAZE_SEASONS.format(tvmaze_id)) as resp:
            if resp.status != 200:
                return 24  # fallback
            seasons = await resp.json()
        
        if not seasons:
            return 24
        
        # Берём последний сезон
        last_season = seasons[-1]
        season_id = last_season['id']
        
        # Получаем эпизоды последнего сезона
        async with session.get(f"https://api.tvmaze.com/seasons/{season_id}/episodes") as ep_resp:
            if ep_resp.status != 200:
                return last_season.get('episodeOrder', 24) or 24
            episodes = await ep_resp.json()
            return len(episodes) if episodes else 24

# ====================== УВЕДОМЛЕНИЯ (оставлено без изменений) ======================
async def send_notifications():
    today = datetime.now().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users")
        users = cur.fetchall()

    for (user_id,) in users:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name, tvmaze_id FROM series WHERE user_id = ? AND notifications_enabled = 1 AND completed = 0", (user_id,))
            series_list = cur.fetchall()

        for series_id, name, tvmaze_id in series_list:
            if not tvmaze_id: continue
            next_ep = await get_next_episode(tvmaze_id)
            if not next_ep: continue

            air_date = next_ep.get('airdate')
            if air_date in (today, (datetime.now().date() + timedelta(days=1)).strftime("%Y-%m-%d")):
                season = next_ep.get('season')
                episode = next_ep.get('number')
                summary = next_ep.get('summary', '')[:250].replace('<p>', '').replace('</p>', '')

                text = f"🔔 **Новая серия скоро!**\n\n🎬 {name}\nS{season}E{episode} — {air_date}\n\n{summary}"
                try:
                    await bot.send_message(user_id, text, parse_mode="Markdown")
                except:
                    pass

def schedule_notifications():
    scheduler.remove_all_jobs()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, notification_hour, notification_minute FROM users")
        for user_id, hour, minute in cur.fetchall():
            scheduler.add_job(send_notifications, CronTrigger(hour=hour, minute=minute), id=f"notif_{user_id}", replace_existing=True)
    print("✅ Планировщик уведомлений обновлён")

# ====================== ДОБАВЛЕНИЕ СЕРИАЛА С АВТОИСПРАВЛЕНИЕМ ======================
@dp.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.add_name)
    await message.answer("📝 Введи название сериала (как ты его знаешь):")

# ... (process_add_name, process_original_name, skip_original — остаются без изменений)

async def try_search_tvmaze(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user_name = data["name"]
    original = data.get("original_name")

    show = None
    used_query = None

    if original:
        show = await search_tvmaze(original)
        used_query = original
    if not show:
        show = await search_tvmaze(user_name)
        used_query = user_name

    if not show:
        await message.answer("❌ Не удалось найти сериал. Попробуй другое название.")
        await state.clear()
        return

    # === НОВОЕ: Автоматическое получение реального количества эпизодов ===
    real_episodes = await get_seasons_episode_count(show['id'])
    
    await state.update_data(
        tvmaze_id=show['id'], 
        tvmaze_name=show['name'], 
        search_used=used_query,
        episodes_per_season=real_episodes
    )

    correction_text = f"\n\nКоличество эпизодов в сезоне автоматически исправлено на **{real_episodes}** (по данным TVMaze)." if real_episodes else ""

    text = (f"✅ Найден сериал по запросу «{used_query}»:\n\n"
            f"<b>{show['name']}</b>\n"
            f"Премьера: {show.get('premiered', '—')}\n"
            f"Статус: {show.get('status', '—')}{correction_text}\n\n"
            f"Добавить этот сериал?")

    await state.set_state(BotStates.add_confirm_tvmaze)
    await message.answer(text, parse_mode="HTML", reply_markup=confirm_keyboard())

@dp.callback_query(F.data == "confirm_add", BotStates.add_confirm_tvmaze)
async def confirm_add(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = callback.from_user.id
    episodes_count = data.get("episodes_per_season", 24)

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO series (user_id, name, original_name, tvmaze_id, episodes_per_season)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, data["name"], data.get("original_name"), data.get("tvmaze_id"), episodes_count))
        conn.commit()

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()

    await state.clear()
    await callback.message.edit_text(f"✅ Сериал «{data['name']}» успешно добавлен!\nКоличество эпизодов в сезоне: {episodes_count}")
    await cmd_my(callback.message)

# Остальные функции (cmd_my, отметка эпизодов, календарь, уведомления, show_series_menu и т.д.) остаются такими же, как в предыдущей версии.

# ====================== ЗАПУСК ======================
async def main():
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT user_id FROM series")
        for (uid,) in cur.fetchall():
            cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
        conn.commit()

    schedule_notifications()
    print("🤖 Бот запущен — автоматическое исправление количества эпизодов из TVMaze + индивидуальные уведомления")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
