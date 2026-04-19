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

# ====================== ВЕБ-СЕРВЕР ======================
async def health_handler(request):
    return web.Response(text="Bot is running", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    print("🌐 Веб-сервер запущен на порту 8080")

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
                print(f"✅ Добавлена колонка {col}")
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

# ====================== API ФУНКЦИИ ======================
async def search_tmdb(query: str):
    if not TMDB_API_KEY or not query: return None
    params = {"api_key": TMDB_API_KEY, "query": query, "language": "ru-RU"}
    async with aiohttp.ClientSession() as session:
        async with session.get(TMDB_SEARCH, params=params) as resp:
            if resp.status != 200: return None
            data = await resp.json()
            return data.get("results")[0] if data.get("results") else None

async def get_tmdb_details(tmdb_id: int):
    if not TMDB_API_KEY: return None
    url = TMDB_DETAILS.format(tmdb_id=tmdb_id)
    params = {"api_key": TMDB_API_KEY, "language": "ru-RU", "append_to_response": "external_ids"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200: return None
            return await resp.json()

async def search_kinopoisk(query: str):
    if not KINOPOISK_API_KEY or not query: return None
    headers = {"X-API-KEY": KINOPOISK_API_KEY}
    params = {"keyword": query, "page": 1}
    async with aiohttp.ClientSession() as session:
        async with session.get(KINOPOISK_SEARCH, headers=headers, params=params) as resp:
            if resp.status != 200: return None
            data = await resp.json()
            return data.get("items")[0] if data.get("items") else None

async def get_kinopoisk_details(kp_id: int):
    if not KINOPOISK_API_KEY: return None
    headers = {"X-API-KEY": KINOPOISK_API_KEY}
    url = KINOPOISK_DETAILS.format(kp_id=kp_id)
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200: return None
            return await resp.json()

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
            season_ep_count = defaultdict(int)
            for ep in episodes:
                if ep.get('season') is not None:
                    season_ep_count[ep['season']] += 1
            return max(season_ep_count.values()) if season_ep_count else 24

async def download_poster_silently(file_url: str | None, user_id: int) -> str | None:
    if not file_url: return None
    try:
        msg = await bot.send_photo(chat_id=user_id, photo=file_url, disable_notification=True)
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
        cur.execute("SELECT episodes_per_season, start_season FROM series WHERE id = ?", (series_id,))
        row = cur.fetchone()
        eps = row[0] or 24 if row else 24
        start_season = row[1] or 1 if row else 1
        cur.execute("SELECT season, COUNT(episode) FROM watched_episodes WHERE series_id = ? GROUP BY season", (series_id,))
        watched_dict = dict(cur.fetchall())
        cur.execute("SELECT season FROM finished_seasons WHERE series_id = ?", (series_id,))
        finished = {row[0] for row in cur.fetchall()}
        seasons = sorted(set(watched_dict.keys()) | finished | {start_season})
        return [(s, watched_dict.get(s, 0), eps, " ✅" if s in finished or watched_dict.get(s, 0) >= eps else "") for s in seasons]

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
        except:
            pass

def schedule_notifications():
    scheduler.remove_all_jobs()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, notification_hour, notification_minute FROM users")
        for user_id, hour, minute in cur.fetchall():
            scheduler.add_job(send_user_notifications, CronTrigger(hour=hour, minute=minute), args=(user_id,), id=f"notif_{user_id}", replace_existing=True)
    print("✅ Планировщик уведомлений обновлён")

# ====================== FSM ======================
class BotStates(StatesGroup):
    add_name = State()
    add_original_name = State()
    add_start_season = State()
    select_season_mark = State()
    select_season_finish = State()

# ====================== ДОБАВЛЕНИЕ СЕРИАЛА ======================
@dp.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.add_name)
    await message.answer("📝 Введи название сериала:")

@dp.message(BotStates.add_name)
async def process_add_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(BotStates.add_original_name)
    await message.answer("Оригинальное название (если знаешь):", reply_markup=skip_keyboard())

@dp.message(BotStates.add_original_name)
async def process_original_name(message: types.Message, state: FSMContext):
    await state.update_data(original_name=message.text.strip())
    await try_search_series(message, state)

@dp.callback_query(F.data == "skip_original", BotStates.add_original_name)
async def skip_original(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(original_name=None)
    await callback.answer("Пропущено")
    await try_search_series(callback.message, state)

async def try_search_series(message: types.Message, state: FSMContext):
    data = await state.get_data()
    search_query = data.get("original_name") or data["name"]

    tmdb_result = await search_tmdb(search_query)
    if tmdb_result:
        tmdb_id = tmdb_result['id']
        details = await get_tmdb_details(tmdb_id)
        kp_id = tvmaze_id = None
        if details and details.get('external_ids'):
            kp_id = details['external_ids'].get('kinopoisk_id') or details['external_ids'].get('kp_id')
            tvmaze_id = details['external_ids'].get('tvmaze_id')
        poster_url = f"https://image.tmdb.org/t/p/original{tmdb_result.get('poster_path')}" if tmdb_result.get('poster_path') else None
        await state.update_data(tmdb_id=tmdb_id, kinopoisk_id=kp_id, tvmaze_id=tvmaze_id, poster_url=poster_url, found_name=tmdb_result.get('name'))
        await process_start_season_choice(message, state)
        return

    kp_result = await search_kinopoisk(search_query)
    if kp_result:
        kp_id = kp_result['kinopoiskId']
        details = await get_kinopoisk_details(kp_id)
        poster_url = details.get('posterUrl') if details else kp_result.get('posterUrl')
        name = kp_result.get('nameRu') or kp_result.get('nameOriginal')
        await state.update_data(kinopoisk_id=kp_id, poster_url=poster_url, found_name=name)
        await process_start_season_choice(message, state)
        return

    tvmaze_show = await search_tvmaze(search_query)
    if tvmaze_show:
        poster_url = tvmaze_show.get('image', {}).get('original')
        await state.update_data(tvmaze_id=tvmaze_show['id'], poster_url=poster_url, found_name=tvmaze_show.get('name'))
        await process_start_season_choice(message, state)
        return

    await message.answer("❌ Сериал не найден.")
    await state.clear()

async def process_start_season_choice(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data.get("found_name") or data.get("name", "Сериал")
    await state.set_state(BotStates.add_start_season)
    await message.answer(f"✅ Найден: <b>{name}</b>\nС какого сезона начинать просмотр?", parse_mode="HTML")

@dp.message(BotStates.add_start_season)
async def process_start_season(message: types.Message, state: FSMContext):
    try:
        start_season = int(message.text.strip())
        if start_season < 1: raise ValueError
    except ValueError:
        await message.answer("❌ Введи корректный номер сезона (от 1)")
        return
    await state.update_data(start_season=start_season)
    await confirm_add_series(message, state)

async def confirm_add_series(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    name = data.get("found_name") or data["name"]
    original_name = data.get("original_name")
    tmdb_id = data.get("tmdb_id")
    kinopoisk_id = data.get("kinopoisk_id")
    tvmaze_id = data.get("tvmaze_id")
    start_season = data.get("start_season", 1)
    poster_url = data.get("poster_url")

    episodes_per_season = await get_episodes_per_season(tvmaze_id) if tvmaze_id else 24
    poster_file_id = await download_poster_silently(poster_url, user_id)

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO series (user_id, name, original_name, tmdb_id, kinopoisk_id, tvmaze_id,
                               poster_file_id, episodes_per_season, start_season)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, name, original_name, tmdb_id, kinopoisk_id, tvmaze_id,
              poster_file_id, episodes_per_season, start_season))
        series_id = cur.lastrowid

        if start_season > 1:
            for s in range(1, start_season):
                for ep in range(1, episodes_per_season + 1):
                    cur.execute("INSERT OR IGNORE INTO watched_episodes (series_id, season, episode) VALUES (?, ?, ?)",
                                (series_id, s, ep))
        conn.commit()

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()

    await state.clear()
    await message.answer(f"✅ Сериал «{name}» добавлен!\nНачинаем с сезона {start_season}")
    await cmd_my(message)

# ====================== МОИ СЕРИАЛЫ ======================
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
        caption = f"🎬 <b>{name}</b>\n📅 Выход: {airing_str}\n👁 Просмотрено всего: {total_watched}\n\n" + "\n".join(progress_lines)
        markup = series_keyboard(sid, completed, notif)

        if poster:
            try:
                await message.answer_photo(photo=poster, caption=caption, parse_mode="HTML", reply_markup=markup)
            except:
                await message.answer(caption, parse_mode="HTML", reply_markup=markup)
        else:
            await message.answer(caption, parse_mode="HTML", reply_markup=markup)

# ====================== ОТМЕТКА ЭПИЗОДОВ ======================
@dp.callback_query(F.data.startswith("mark_episodes_"))
async def start_mark_episodes(callback: types.CallbackQuery, state: FSMContext):
    series_id = int(callback.data.split("_")[-1])
    await state.update_data(series_id=series_id)
    await state.set_state(BotStates.select_season_mark)
    try:
        await callback.message.edit_text("Введите номер сезона для отметки эпизодов:")
    except TelegramBadRequest:
        await callback.message.answer("Введите номер сезона для отметки эпизодов:")
    await callback.answer()

@dp.message(BotStates.select_season_mark)
async def process_select_season_mark(message: types.Message, state: FSMContext):
    try:
        season = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введи число!")
        return
    data = await state.get_data()
    series_id = data["series_id"]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT episodes_per_season FROM series WHERE id = ?", (series_id,))
        eps = cur.fetchone()[0] or 24
    await state.update_data(current_season=season, total_episodes=eps)
    await message.answer(f"🎬 Отмечай эпизоды сезона {season}:", reply_markup=episodes_keyboard(series_id, season, eps))

@dp.callback_query(F.data.startswith("toggle_ep_"))
async def toggle_episode(callback: types.CallbackQuery, state: FSMContext):
    try:
        parts = callback.data.split("_")
        series_id = int(parts[2])
        season = int(parts[3])
        episode = int(parts[4])
    except (IndexError, ValueError):
        await callback.answer("Ошибка данных кнопки", show_alert=True)
        return

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO watched_episodes (series_id, season, episode) VALUES (?,?,?)", 
                    (series_id, season, episode))
        if cur.rowcount == 0:
            cur.execute("DELETE FROM watched_episodes WHERE series_id=? AND season=? AND episode=?", 
                        (series_id, season, episode))
        conn.commit()

    data = await state.get_data()
    total = data.get("total_episodes", 24)
    try:
        await callback.message.edit_reply_markup(reply_markup=episodes_keyboard(series_id, season, total))
    except TelegramBadRequest:
        pass
    await callback.answer(f"S{season}E{episode}")

@dp.callback_query(F.data.startswith("finish_marking_"))
async def finish_marking(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("✅ Сохранено!", show_alert=True)
    await state.clear()
    series_id = int(callback.data.split("_")[-1])
    await show_series_menu(callback.message, series_id)

# ====================== ЗАВЕРШЕНИЕ СЕЗОНА ======================
@dp.callback_query(F.data.startswith("finish_season_"))
async def start_finish_season(callback: types.CallbackQuery, state: FSMContext):
    series_id = int(callback.data.split("_")[-1])
    await state.update_data(series_id=series_id)
    await state.set_state(BotStates.select_season_finish)
    try:
        await callback.message.edit_text("Введите номер сезона для завершения:")
    except TelegramBadRequest:
        await callback.message.answer("Введите номер сезона для завершения:")
    await callback.answer()

@dp.message(BotStates.select_season_finish)
async def process_finish_season(message: types.Message, state: FSMContext):
    try:
        season = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введи число!")
        return
    data = await state.get_data()
    series_id = data["series_id"]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO finished_seasons (series_id, season) VALUES (?, ?)", (series_id, season))
        conn.commit()
    await state.clear()
    await message.answer(f"✅ Сезон {season} завершён!")
    await show_series_menu(message, series_id)

# ====================== ОСТАЛЬНЫЕ ХЕНДЛЕРЫ ======================
@dp.callback_query(F.data.startswith("calendar_"))
async def show_calendar(callback: types.CallbackQuery):
    series_id = int(callback.data.split("_")[1])
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name, tvmaze_id FROM series WHERE id = ?", (series_id,))
        row = cur.fetchone()
        if not row:
            await callback.answer("Сериал не найден", show_alert=True)
            return
        name, tvmaze_id = row
    if not tvmaze_id:
        await callback.answer("Календарь недоступен", show_alert=True)
        return
    next_ep = await get_next_episode(tvmaze_id)
    text = f"🎬 <b>{name}</b>\n\nСериал завершён или новых серий пока нет." if not next_ep else \
           f"📅 <b>Календарь — {name}</b>\n\nСледующая серия: S{next_ep.get('season')}E{next_ep.get('number')}\nДата: {next_ep.get('airdate')}"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=series_keyboard(series_id))
    await callback.answer()

@dp.callback_query(F.data.startswith("toggle_notif_"))
async def toggle_notifications(callback: types.CallbackQuery):
    series_id = int(callback.data.split("_")[-1])
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT notifications_enabled FROM series WHERE id = ?", (series_id,))
        enabled = cur.fetchone()[0] or 1
        new_state = 0 if enabled else 1
        cur.execute("UPDATE series SET notifications_enabled = ? WHERE id = ?", (new_state, series_id))
        conn.commit()
    await callback.answer(f"Уведомления {'включены 🔔' if new_state else 'выключены 🔕'}", show_alert=True)
    await show_series_menu(callback.message, series_id)

@dp.callback_query(F.data.startswith("complete_"))
async def complete_series(callback: types.CallbackQuery):
    series_id = int(callback.data.split("_")[-1])
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE series SET completed = 1 WHERE id = ?", (series_id,))
        conn.commit()
    await callback.answer("✅ Сериал завершён", show_alert=True)
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
    try:
        await callback.message.delete()
    except:
        pass

@dp.callback_query(F.data == "change_time")
async def change_notification_time(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text("Выбери удобное время для уведомлений:", reply_markup=notification_time_keyboard())
    except TelegramBadRequest:
        await callback.message.answer("Выбери удобное время для уведомлений:", reply_markup=notification_time_keyboard())
    await callback.answer()

# ====================== ИСПРАВЛЕННЫЙ ХЕНДЛЕР ВРЕМЕНИ ======================
@dp.callback_query(F.data.startswith("set_time_"))
async def set_user_time(callback: types.CallbackQuery):
    try:
        parts = callback.data.split("_")
        # parts = ['set', 'time', '14', '0']
        hour = int(parts[2])
        minute = int(parts[3])
    except (IndexError, ValueError):
        await callback.answer("Ошибка данных времени", show_alert=True)
        return

    user_id = callback.from_user.id
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (user_id, notification_hour, notification_minute)
            VALUES (?, ?, ?) 
            ON CONFLICT(user_id) DO UPDATE SET
                notification_hour = excluded.notification_hour,
                notification_minute = excluded.notification_minute
        """, (user_id, hour, minute))
        conn.commit()

    await callback.answer(f"Время уведомлений: {hour:02d}:{minute:02d}", show_alert=True)
    schedule_notifications()
    await cmd_my(callback.message)

@dp.callback_query(F.data == "cancel_marking")
async def cancel_marking(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.delete()
    except:
        pass
    await callback.answer("Отменено")

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
    caption = f"🎬 <b>{name}</b>\n📅 Выход: {airing_str}\n👁 Просмотрено всего: {total_watched}\n\n" + "\n".join(progress_lines)
    markup = series_keyboard(series_id, completed, notif)

    try:
        await message.edit_text(caption, parse_mode="HTML", reply_markup=markup)
    except TelegramBadRequest:
        if poster:
            await message.answer_photo(photo=poster, caption=caption, parse_mode="HTML", reply_markup=markup)
        else:
            await message.answer(caption, parse_mode="HTML", reply_markup=markup)

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

    print("🚀 Бот запущен — ошибка unpack в set_user_time исправлена")
    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    )

if __name__ == "__main__":
    asyncio.run(main())
