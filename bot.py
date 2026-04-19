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

scheduler = AsyncIOScheduler()

class BotStates(StatesGroup):
    add_name = State()
    add_original_name = State()
    add_confirm_tvmaze = State()
    select_season_mark = State()
    select_season_finish = State()
    mark_multiple_episodes = State()
    set_notification_time = State()

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
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить", callback_data="skip_original")]
    ])

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
        row.append(types.InlineKeyboardButton(
            text=f"{status} {ep}", 
            callback_data=f"toggle_ep_{series_id}_{season}_{ep}"
        ))
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

async def get_next_episode(tvmaze_id: int):
    if not tvmaze_id: return None
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.tvmaze.com/shows/{tvmaze_id}/nextepisode") as resp:
            if resp.status in (404, 204): return None
            if resp.status != 200: return None
            return await resp.json()

# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
def get_watched_count(series_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM watched_episodes WHERE series_id = ?", (series_id,))
        return cur.fetchone()[0]

def get_active_seasons_progress(series_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT episodes_per_season FROM series WHERE id = ?", (series_id,))
        eps = cur.fetchone()[0] or 24

        cur.execute("SELECT season, COUNT(episode) FROM watched_episodes WHERE series_id = ? GROUP BY season", (series_id,))
        watched_dict = dict(cur.fetchall())

        cur.execute("SELECT season FROM finished_seasons WHERE series_id = ?", (series_id,))
        finished = {row[0] for row in cur.fetchall()}

        seasons = sorted(set(watched_dict.keys()) | finished | {1})
        return [(s, watched_dict.get(s, 0), eps, " ✅" if s in finished or watched_dict.get(s, 0) >= eps else "") for s in seasons]

# ====================== УВЕДОМЛЕНИЯ ======================
async def send_notifications():
    today = datetime.now().strftime("%Y-%m-%d")
    
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users")
        users = cur.fetchall()

    for (user_id,) in users:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, name, tvmaze_id 
                FROM series 
                WHERE user_id = ? AND notifications_enabled = 1 AND completed = 0
            """, (user_id,))
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

                text = (f"🔔 **Новая серия скоро!**\n\n"
                        f"🎬 {name}\n"
                        f"S{season}E{episode} — {air_date}\n\n"
                        f"{summary}")

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
            scheduler.add_job(
                send_notifications,
                CronTrigger(hour=hour, minute=minute),
                id=f"notif_{user_id}",
                replace_existing=True
            )
    print("✅ Планировщик уведомлений обновлён")

# ====================== ДОБАВЛЕНИЕ СЕРИАЛА ======================
@dp.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.add_name)
    await message.answer("📝 Введи название сериала (как ты его знаешь):")

@dp.message(BotStates.add_name)
async def process_add_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(BotStates.add_original_name)
    await message.answer(
        "Если знаешь оригинальное название (на английском) — введи его.\n"
        "Это сильно улучшит поиск в TVMaze.\n\n"
        "Если не знаешь — нажми кнопку ниже:",
        reply_markup=skip_keyboard()
    )

@dp.message(BotStates.add_original_name)
async def process_original_name(message: types.Message, state: FSMContext):
    await state.update_data(original_name=message.text.strip())
    await try_search_tvmaze(message, state)

@dp.callback_query(F.data == "skip_original", BotStates.add_original_name)
async def skip_original(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(original_name=None)
    await callback.answer("Пропущено")
    await try_search_tvmaze(callback.message, state)

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

    await state.update_data(tvmaze_id=show['id'], tvmaze_name=show['name'], search_used=used_query)

    text = (f"✅ Найден сериал по запросу «{used_query}»:\n\n"
            f"<b>{show['name']}</b>\n"
            f"Премьера: {show.get('premiered', '—')}\n"
            f"Статус: {show.get('status', '—')}\n\n"
            f"Добавить этот сериал?")

    await state.set_state(BotStates.add_confirm_tvmaze)
    await message.answer(text, parse_mode="HTML", reply_markup=confirm_keyboard())

@dp.callback_query(F.data == "confirm_add", BotStates.add_confirm_tvmaze)
async def confirm_add(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = callback.from_user.id

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO series (user_id, name, original_name, tvmaze_id, episodes_per_season)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, data["name"], data.get("original_name"), data.get("tvmaze_id"), 24))
        conn.commit()

    # Создаём запись пользователя, если её нет
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()

    await state.clear()
    await callback.message.edit_text(f"✅ Сериал «{data['name']}» успешно добавлен!")
    await cmd_my(callback.message)

@dp.callback_query(F.data == "cancel_add")
async def cancel_add(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Добавление отменено.")
    await callback.answer()

# ====================== МОИ СЕРИАЛЫ ======================
@dp.message(Command("my"))
async def cmd_my(message: types.Message):
    user_id = message.from_user.id
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name, poster_file_id, airing_days, completed, notifications_enabled FROM series WHERE user_id = ?", (user_id,))
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
            await message.answer_photo(photo=poster, caption=caption, parse_mode="HTML", reply_markup=markup)
        else:
            await message.answer(caption, parse_mode="HTML", reply_markup=markup)

# ====================== ОТМЕТКА ЭПИЗОДОВ ======================
@dp.callback_query(F.data.startswith("mark_episodes_"))
async def start_mark_episodes(callback: types.CallbackQuery, state: FSMContext):
    series_id = int(callback.data.split("_")[-1])
    await state.update_data(series_id=series_id)
    await state.set_state(BotStates.select_season_mark)
    await callback.message.edit_text("Введите номер сезона для отметки эпизодов:")
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

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT episodes_per_season FROM series WHERE id = ?", (series_id,))
        eps = cur.fetchone()[0] or 24

    await state.update_data(current_season=season, total_episodes=eps)
    await state.set_state(BotStates.mark_multiple_episodes)

    await message.answer(f"🎬 Отмечай эпизоды сезона {season}:", reply_markup=episodes_keyboard(series_id, season, eps))

@dp.callback_query(F.data.startswith("toggle_ep_"))
async def toggle_episode(callback: types.CallbackQuery, state: FSMContext):
    _, series_id_str, season_str, ep_str = callback.data.split("_")
    series_id = int(series_id_str)
    season = int(season_str)
    episode = int(ep_str)

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO watched_episodes (series_id, season, episode) VALUES (?, ?, ?)",
                    (series_id, season, episode))
        if cur.rowcount == 0:
            cur.execute("DELETE FROM watched_episodes WHERE series_id=? AND season=? AND episode=?", 
                        (series_id, season, episode))
        conn.commit()

    data = await state.get_data()
    total = data.get("total_episodes", 24)

    try:
        await callback.message.edit_reply_markup(reply_markup=episodes_keyboard(series_id, season, total))
    except:
        pass
    await callback.answer(f"✅ S{season}E{episode}")

@dp.callback_query(F.data.startswith("finish_marking_"))
async def finish_marking(callback: types.CallbackQuery, state: FSMContext):
    series_id = int(callback.data.split("_")[-1])
    await callback.answer(f"✅ Сохранено! Всего просмотрено: {get_watched_count(series_id)}", show_alert=True)
    await state.clear()
    await show_series_menu(callback.message, series_id)

@dp.callback_query(F.data == "cancel_marking")
async def cancel_marking(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.delete()
    except:
        pass
    await callback.answer("Отменено")

# ====================== КАЛЕНДАРЬ И УПРАВЛЕНИЕ ======================
@dp.callback_query(F.data.startswith("calendar_"))
async def show_calendar(callback: types.CallbackQuery):
    series_id = int(callback.data.split("_")[1])
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT name, tvmaze_id FROM series WHERE id = ?", (series_id,))
        name, tvmaze_id = cur.fetchone() or (None, None)

    if not tvmaze_id:
        await callback.answer("Календарь недоступен", show_alert=True)
        return

    next_ep = await get_next_episode(tvmaze_id)
    if not next_ep:
        text = f"🎬 <b>{name}</b>\n\nСериал завершён или новых серий пока нет."
    else:
        air_date = next_ep.get('airdate', '—')
        air_time = next_ep.get('airtime', '')
        season = next_ep.get('season')
        episode = next_ep.get('number')
        summary = next_ep.get('summary', 'Нет описания')[:300].replace('<p>', '').replace('</p>', '')

        text = (f"📅 <b>Календарь — {name}</b>\n\n"
                f"Следующая серия: S{season}E{episode}\n"
                f"Дата: {air_date} {air_time}\n\n"
                f"{summary}")

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=series_keyboard(series_id))
    await callback.answer()

@dp.callback_query(F.data.startswith("finish_season_"))
async def start_finish_season(callback: types.CallbackQuery, state: FSMContext):
    series_id = int(callback.data.split("_")[-1])
    await state.update_data(series_id=series_id)
    await state.set_state(BotStates.select_season_finish)
    await callback.message.edit_text("Введите номер сезона, который хотите завершить:")
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

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO finished_seasons (series_id, season) VALUES (?, ?)", (series_id, season))
        conn.commit()

    await state.clear()
    await message.answer(f"✅ Сезон {season} завершён!")
    await show_series_menu(message, series_id)

@dp.callback_query(F.data.startswith("toggle_notif_"))
async def toggle_notifications(callback: types.CallbackQuery):
    series_id = int(callback.data.split("_")[-1])
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT notifications_enabled FROM series WHERE id = ?", (series_id,))
        enabled = cur.fetchone()[0] or 1
        new_state = 0 if enabled else 1
        cur.execute("UPDATE series SET notifications_enabled = ? WHERE id = ?", (new_state, series_id))
        conn.commit()

    status = "включены 🔔" if new_state else "выключены 🔕"
    await callback.answer(f"Уведомления {status}", show_alert=True)
    await show_series_menu(callback.message, series_id)

@dp.callback_query(F.data == "change_time")
async def change_notification_time(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "Выбери удобное время для ежедневных уведомлений:",
        reply_markup=notification_time_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("set_time_"))
async def set_user_time(callback: types.CallbackQuery):
    _, hour, minute = callback.data.split("_")
    hour, minute = int(hour), int(minute)
    user_id = callback.from_user.id

    with sqlite3.connect(DB_PATH) as conn:
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
    await show_series_menu(callback.message, series_id=0)  # обновим меню

async def show_series_menu(message: types.Message, series_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT name, poster_file_id, airing_days, completed, notifications_enabled FROM series WHERE id = ?", (series_id,))
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

    try:
        await message.edit_text(caption, parse_mode="HTML", reply_markup=series_keyboard(series_id, completed, notif))
    except Exception:
        if poster:
            await message.answer_photo(photo=poster, caption=caption, parse_mode="HTML", reply_markup=series_keyboard(series_id, completed, notif))
        else:
            await message.answer(caption, parse_mode="HTML", reply_markup=series_keyboard(series_id, completed, notif))

# ====================== ЗАПУСК ======================
async def main():
    init_db()

    # Инициализация пользователей
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT user_id FROM series")
        for (uid,) in cur.fetchall():
            cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
        conn.commit()

    schedule_notifications()
    print("🤖 Бот полностью запущен с индивидуальными уведомлениями!")

    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
