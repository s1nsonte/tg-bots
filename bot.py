import asyncio
import logging
import sqlite3
import os

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN is missing or empty!")

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ====================== ПУТЬ К БАЗЕ ======================
DATA_DIR = "/data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "series_bot.db")

print(f"✅ База данных: {DB_PATH}")


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.executescript('''
            CREATE TABLE IF NOT EXISTS series (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT NOT NULL,
                poster_file_id TEXT,
                airing_days TEXT,
                episodes_per_season INTEGER,
                completed BOOLEAN DEFAULT FALSE
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


class BotStates(StatesGroup):
    add_name = State()
    add_poster = State()
    choose_days = State()
    enter_episodes_count = State()
    input_season_episode = State()
    input_season_finish = State()
    mark_multiple_season = State()      # ввод сезона
    mark_multiple_episodes = State()    # выбор серий


# ====================== КЛАВИАТУРЫ ======================
def day_keyboard(selected_days=None):
    if selected_days is None: selected_days = []
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    buttons = [[types.InlineKeyboardButton(
        text=f"{'🟢' if str(i) in selected_days else '🔳'} {day}",
        callback_data=f"day_{i}"
    )] for i, day in enumerate(days)]
    buttons.append([types.InlineKeyboardButton(text="Готово ✅", callback_data="done_days")])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


def episodes_keyboard(season: int, total: int):
    rows = [[types.InlineKeyboardButton(text=str(ep), callback_data=f"ep_{season}_{ep}")] 
            for ep in range(1, total + 1)]
    rows.append([types.InlineKeyboardButton(text="Назад ↩️", callback_data="back_to_menu")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def series_keyboard(series_id: int, completed: bool = False):
    keyboard = [
        [types.InlineKeyboardButton(text="✅ Отметить эпизод", callback_data=f"watch_{series_id}")],
        [types.InlineKeyboardButton(text="🏁 Завершить сезон", callback_data=f"finish_{series_id}")],
        [types.InlineKeyboardButton(text="Отметить несколько серий", callback_data=f"mark_episodes_{series_id}")],
    ]
    if not completed:
        keyboard.append([types.InlineKeyboardButton(text="Завершить сериал 🆕", callback_data=f"complete_{series_id}")])
    keyboard.append([types.InlineKeyboardButton(text="Удалить сериал 🗑", callback_data=f"delete_{series_id}")])
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


# ====================== ОСНОВНЫЕ КОМАНДЫ ======================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("👋 Бот трекинга сериалов\n\n/add — добавить\n/my — мои сериалы")


@dp.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.add_name)
    await message.answer("📝 Название сериала:")


# ... (добавление сериала остаётся прежним, для краткости опущено ниже)


@dp.message(Command("my"))
async def cmd_my(message: types.Message):
    user_id = message.from_user.id
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name, poster_file_id, airing_days, completed, episodes_per_season FROM series WHERE user_id = ?", (user_id,))
        series_list = cur.fetchall()

    if not series_list:
        await message.answer("У тебя пока нет сериалов.")
        return

    days_map = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    for sid, name, poster, airing_days, completed, eps in series_list:
        airing_str = ', '.join(days_map[int(d)] for d in airing_days.split(',')) if airing_days else "—"
        cur.execute("SELECT COUNT(*) FROM watched_episodes WHERE series_id = ?", (sid,))
        watched = cur.fetchone()[0]
        cur.execute("SELECT season FROM finished_seasons WHERE series_id = ?", (sid,))
        finished_str = ", ".join(str(row[0]) for row in cur.fetchall()) or "нет"

        caption = f"🎬 <b>{name}</b>\n📅 Выход: {airing_str}\n👁 Просмотрено: {watched}\n🏁 Сезоны: {finished_str}"

        if poster:
            await message.answer_photo(photo=poster, caption=caption, parse_mode="HTML", reply_markup=series_keyboard(sid, completed))
        else:
            await message.answer(caption, parse_mode="HTML", reply_markup=series_keyboard(sid, completed))


# ====================== МАССОВАЯ ОТМЕТКА СЕРИЙ (ИСПРАВЛЕНО) ======================
@dp.callback_query(F.data.startswith("mark_episodes_"))
async def start_mark_episodes(callback: types.CallbackQuery, state: FSMContext):
    series_id = int(callback.data.split("_")[1])
    await state.update_data(series_id=series_id)

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT episodes_per_season FROM series WHERE id = ?", (series_id,))
        episodes_count = cur.fetchone()[0]

    if episodes_count and episodes_count > 0:
        await state.update_data(total_episodes=episodes_count)
        await state.set_state(BotStates.mark_multiple_episodes)
        await callback.message.answer(f"Выбери серии сезона 1:", 
                                      reply_markup=episodes_keyboard(1, episodes_count))
    else:
        await state.set_state(BotStates.mark_multiple_season)
        await callback.message.answer("Напиши номер сезона для отметки серий:")
    
    await callback.answer()


@dp.message(BotStates.mark_multiple_season)
async def process_mark_season(message: types.Message, state: FSMContext):
    try:
        season = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введи только число!")
        return

    await state.update_data(current_season=season)
    await state.set_state(BotStates.mark_multiple_episodes)
    await message.answer(f"Выбери серии сезона {season}:", 
                         reply_markup=episodes_keyboard(season, 30))  # можно увеличить лимит
    await message.delete()


@dp.callback_query(F.data.startswith("ep_"))
async def mark_episode(callback: types.CallbackQuery, state: FSMContext):
    _, season_str, ep_str = callback.data.split("_")
    season = int(season_str)
    episode = int(ep_str)

    data = await state.get_data()
    series_id = data["series_id"]

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watched_episodes (series_id, season, episode) VALUES (?, ?, ?)",
            (series_id, season, episode)
        )

    # Обновляем клавиатуру
    try:
        total = data.get("total_episodes", 30)
        await callback.message.edit_reply_markup(reply_markup=episodes_keyboard(season, total))
    except:
        pass

    await callback.answer(f"✅ S{season}E{episode} отмечено")


@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.answer("Вернулись в меню")


# ====================== ЗАПУСК ======================
async def main():
    init_db()
    print("🤖 Бот запущен!")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
