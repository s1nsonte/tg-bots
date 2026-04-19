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
    mark_multiple_season = State()
    mark_multiple_episodes = State()


# ====================== КЛАВИАТУРЫ ======================
def day_keyboard(selected_days=None):
    if selected_days is None:
        selected_days = []
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    buttons = [[types.InlineKeyboardButton(text=f"{'🟢' if str(i) in selected_days else '🔳'} {day}", 
                                           callback_data=f"day_{i}")] for i, day in enumerate(days)]
    buttons.append([types.InlineKeyboardButton(text="Готово ✅", callback_data="done_days")])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


def episodes_keyboard(season: int, total: int):
    rows = [[types.InlineKeyboardButton(text=str(ep), callback_data=f"ep_{season}_{ep}")] for ep in range(1, total+1)]
    rows.append([types.InlineKeyboardButton(text="Назад ↩️", callback_data="back_to_menu")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def series_keyboard(series_id: int, completed: bool = False):
    kb = [
        [types.InlineKeyboardButton(text="✅ Отметить эпизод", callback_data=f"watch_{series_id}")],
        [types.InlineKeyboardButton(text="🏁 Завершить сезон", callback_data=f"finish_{series_id}")],
        [types.InlineKeyboardButton(text="Отметить несколько серий", callback_data=f"mark_episodes_{series_id}")],
    ]
    if not completed:
        kb.append([types.InlineKeyboardButton(text="Завершить сериал 🆕", callback_data=f"complete_{series_id}")])
    kb.append([types.InlineKeyboardButton(text="Удалить сериал 🗑", callback_data=f"delete_{series_id}")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


# ====================== ОСНОВНЫЕ ОБРАБОТЧИКИ ======================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("👋 Привет! Бот для трекинга сериалов.\n\n/add — добавить сериал\n/my — мои сериалы")


@dp.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.add_name)
    await message.answer("📝 Введи название сериала:")


# ... (добавление сериала остаётся таким же, как раньше)
# Чтобы не делать сообщение слишком длинным, я оставлю только критически важные части ниже.

# ====================== КНОПКИ УПРАВЛЕНИЯ СЕРИАЛОМ ======================

@dp.callback_query(F.data.startswith("watch_"))
async def cb_watch(callback: types.CallbackQuery, state: FSMContext):
    series_id = int(callback.data.split("_")[1])
    await state.update_data(series_id=series_id)
    await state.set_state(BotStates.input_season_episode)
    await callback.message.answer("Напиши номер сезона и эпизода через пробел\nПример: <code>2 10</code>", parse_mode="HTML")
    await callback.answer()


@dp.message(BotStates.input_season_episode)
async def process_watch(message: types.Message, state: FSMContext):
    try:
        season, episode = map(int, message.text.strip().split())
    except:
        await message.answer("❌ Неверный формат! Пример: 2 10")
        return

    data = await state.get_data()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO watched_episodes (series_id, season, episode) VALUES (?, ?, ?)",
                     (data['series_id'], season, episode))

    await message.answer(f"✅ S{season}E{episode} отмечен!")
    await state.clear()


@dp.callback_query(F.data.startswith("finish_"))
async def cb_finish(callback: types.CallbackQuery, state: FSMContext):
    series_id = int(callback.data.split("_")[1])
    await state.update_data(series_id=series_id)
    await state.set_state(BotStates.input_season_finish)
    await callback.message.answer("Напиши номер сезона:")
    await callback.answer()


@dp.message(BotStates.input_season_finish)
async def process_finish(message: types.Message, state: FSMContext):
    try:
        season = int(message.text.strip())
    except:
        await message.answer("❌ Введи только число!")
        return

    data = await state.get_data()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO finished_seasons (series_id, season) VALUES (?, ?)",
                     (data['series_id'], season))

    await message.answer(f"🏁 Сезон {season} завершён!")
    await state.clear()


@dp.callback_query(F.data.startswith("complete_"))
async def cb_complete(callback: types.CallbackQuery):
    series_id = int(callback.data.split("_")[1])
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE series SET completed = TRUE WHERE id = ?", (series_id,))

    await callback.message.edit_caption("✅ Сериал отмечен как завершённый", 
                                        reply_markup=series_keyboard(series_id, True))
    await callback.answer("Сериал завершён")


@dp.callback_query(F.data.startswith("delete_"))
async def cb_delete(callback: types.CallbackQuery):
    series_id = int(callback.data.split("_")[1])
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM series WHERE id = ?", (series_id,))
        conn.execute("DELETE FROM watched_episodes WHERE series_id = ?", (series_id,))
        conn.execute("DELETE FROM finished_seasons WHERE series_id = ?", (series_id,))

    await callback.message.delete()
    await callback.answer("Сериал удалён")


@dp.callback_query(F.data.startswith("mark_episodes_"))
async def cb_mark_episodes(callback: types.CallbackQuery, state: FSMContext):
    series_id = int(callback.data.split("_")[1])
    await state.update_data(series_id=series_id)

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT episodes_per_season FROM series WHERE id = ?", (series_id,))
        eps = cur.fetchone()[0]

    if eps:
        await state.set_state(BotStates.mark_multiple_episodes)
        await callback.message.answer("Выбери серии:", reply_markup=episodes_keyboard(1, eps))
    else:
        await state.set_state(BotStates.mark_multiple_season)
        await callback.message.answer("Укажи номер сезона:")
    await callback.answer()


# ====================== ЗАПУСК ======================
async def main():
    init_db()
    print("🤖 Бот запущен...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
