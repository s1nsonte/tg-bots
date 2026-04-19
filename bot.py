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
        row.append(InlineKeyboardButton(
            text=f"{status} {ep}",
            callback_data=f"toggle_ep_{series_id}_{season}_{ep}"
        ))
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


# ====================== TVMAZE ======================
async def search_tvmaze(query: str):
    if not query:
        return None
    async with aiohttp.ClientSession() as session:
        async with session.get(TVMAZE_SEARCH + query.replace(" ", "+")) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return data[0]['show'] if data else None


async def get_next_episode(tvmaze_id: int):
    if not tvmaze_id:
        return None
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.tvmaze.com/shows/{tvmaze_id}/nextepisode") as resp:
            if resp.status in (404, 204):
                return None
            if resp.status != 200:
                return None
            return await resp.json()


async def get_episodes_per_season(tvmaze_id: int) -> int:
    if not tvmaze_id:
        return 24
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.tvmaze.com/shows/{tvmaze_id}/episodes") as resp:
            if resp.status != 200:
                return 24
            try:
                episodes = await resp.json()
            except Exception:
                return 24
            if not episodes:
                return 24

            season_ep_count = defaultdict(int)
            for ep in episodes:
                if ep.get('season') is not None:
                    season_ep_count[ep['season']] += 1
            return max(season_ep_count.values()) if season_ep_count else 24


async def download_poster_silently(tvmaze_show: dict, user_id: int) -> str | None:
    image = tvmaze_show.get('image')
    if not image or not image.get('original'):
        return None

    poster_url = image['original']
    try:
        msg = await bot.send_photo(
            chat_id=user_id,
            photo=poster_url,
            disable_notification=True
        )
        file_id = msg.photo[-1].file_id
        await msg.delete()
        return file_id
    except Exception as e:
        logging.error(f"Не удалось загрузить постер: {e}")
        return None


# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
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
        if not tvmaze_id:
            continue
        next_ep = await get_next_episode(tvmaze_id)
        if not next_ep:
            continue

        air_date = next_ep.get('airdate')
        if air_date not in (today, tomorrow):
            continue

        season = next_ep.get('season')
        episode = next_ep.get('number')
        summary = next_ep.get('summary', '')[:250].replace('<p>', '').replace('</p>', '')

        text = (f"🔔 **Новая серия скоро!**\n\n"
                f"🎬 {name}\n"
                f"S{season}E{episode} — {air_date}\n\n"
                f"{summary}")

        try:
            await bot.send_message(user_id, text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление {user_id}: {e}")


def schedule_notifications():
    scheduler.remove_all_jobs()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, notification_hour, notification_minute FROM users")
        for user_id, hour, minute in cur.fetchall():
            scheduler.add_job(
                send_user_notifications,
                CronTrigger(hour=hour, minute=minute),
                args=(user_id,),
                id=f"notif_{user_id}",
                replace_existing=True
            )
    print("✅ Планировщик уведомлений обновлён")


# ====================== FSM ======================
class BotStates(StatesGroup):
    add_name = State()
    add_original_name = State()
    add_confirm_tvmaze = State()
    select_season_mark = State()
    select_season_finish = State()
    mark_multiple_episodes = State()


# ====================== КОМАНДЫ ======================
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

    await state.update_data(
        tvmaze_id=show['id'],
        tvmaze_name=show['name'],
        search_used=used_query,
        tvmaze_show=show
    )

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
    tvmaze_id = data.get("tvmaze_id")
    tvmaze_show = data.get("tvmaze_show")

    episodes_per_season = await get_episodes_per_season(tvmaze_id)
    poster_file_id = await download_poster_silently(tvmaze_show, user_id) if tvmaze_show else None

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO series (user_id, name, original_name, tvmaze_id, poster_file_id, episodes_per_season)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, data["name"], data.get("original_name"), tvmaze_id, poster_file_id, episodes_per_season))
        conn.commit()

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()

    await state.clear()

    poster_info = "с постером ✅" if poster_file_id else "без постера"
    await callback.message.edit_text(
        f"✅ Сериал «{data['name']}» успешно добавлен!\n"
        f"Эпизодов в сезоне: <b>{episodes_per_season}</b>\n"
        f"Постер: {poster_info}",
        parse_mode="HTML"
    )
    await cmd_my(callback.message)


@dp.callback_query(F.data == "cancel_add")
async def cancel_add(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Добавление отменено.")
    await callback.answer()


@dp.message(Command("my"))
async def cmd_my(message: types.Message):
    user_id = message.from_user.id
    with get_db() as conn:
        cur = conn.cursor()
        # ИСПРАВЛЕННЫЙ ЗАПРОС — добавлена запятая!
        cur.execute("""
            SELECT id, name, poster_file_id, airing_days, completed, notifications_enabled 
            FROM series 
            WHERE user_id = ?
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
            except Exception:
                await message.answer(caption, parse_mode="HTML", reply_markup=markup)
        else:
            await message.answer(caption, parse_mode="HTML", reply_markup=markup)


# ====================== ОСТАЛЬНЫЕ ОБРАБОТЧИКИ (оставлены без изменений) ======================
# (start_mark_episodes, toggle_episode, finish_marking, calendar, finish_season, toggle_notif, change_time и т.д.)

# Чтобы не делать сообщение слишком длинным, я оставил их такими же, как в предыдущей версии.
# Просто замени функцию cmd_my на исправленную выше, и ошибка исчезнет.

# Если хочешь — могу прислать весь файл целиком ещё раз с исправленным cmd_my.

# ====================== ЗАПУСК ======================
@dp.startup()
async def on_startup():
    init_db()

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT user_id FROM series")
        for (uid,) in cur.fetchall():
            cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
        conn.commit()

    scheduler.start()
    schedule_notifications()
    print("🤖 Бот запущен (SQL ошибка исправлена)")


@dp.shutdown()
async def on_shutdown():
    scheduler.shutdown(wait=False)
    await bot.session.close()


async def main():
    print("🚀 Запуск бота...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
