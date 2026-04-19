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
    buttons = [[types.InlineKeyboardButton(
        text=f"{'🟢' if str(i) in selected_days else '🔳'} {day}",
        callback_data=f"day_{i}"
    )] for i, day in enumerate(days)]
    buttons.append([types.InlineKeyboardButton(text="Готово ✅", callback_data="done_days")])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)

def episodes_keyboard(series_id: int, season: int, total: int):
    """Клавиатура с галочками для отметки эпизодов"""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT episode FROM watched_episodes WHERE series_id = ? AND season = ?", 
                    (series_id, season))
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

def series_keyboard(series_id: int, completed: bool = False):
    """Основное меню сериала"""
    keyboard = [
        [types.InlineKeyboardButton(text="📺 Отметить просмотренные эпизоды", 
                                   callback_data=f"mark_episodes_{series_id}")],
        [types.InlineKeyboardButton(text="🏁 Завершить сезон", callback_data=f"finish_{series_id}")],
    ]
    if not completed:
        keyboard.append([types.InlineKeyboardButton(text="Завершить сериал 🆕", 
                                                   callback_data=f"complete_{series_id}")])
    keyboard.append([types.InlineKeyboardButton(text="🗑 Удалить сериал", callback_data=f"delete_{series_id}")])
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)

# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
def get_watched_count(series_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM watched_episodes WHERE series_id = ?", (series_id,))
        return cur.fetchone()[0]

# ====================== ОСНОВНЫЕ КОМАНДЫ ======================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("👋 Бот трекинга сериалов\n\n/add — добавить сериал\n/my — мои сериалы")

@dp.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.add_name)
    await message.answer("📝 Название сериала:")

@dp.message(Command("my"))
async def cmd_my(message: types.Message):
    user_id = message.from_user.id
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, name, poster_file_id, airing_days, completed, episodes_per_season 
            FROM series WHERE user_id = ?
        """, (user_id,))
        series_list = cur.fetchall()

    if not series_list:
        await message.answer("У тебя пока нет сериалов.")
        return

    days_map = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    for sid, name, poster, airing_days, completed, eps_per_season in series_list:
        airing_str = ', '.join(days_map[int(d)] for d in airing_days.split(',')) if airing_days else "—"
        watched = get_watched_count(sid)

        cur.execute("SELECT season FROM finished_seasons WHERE series_id = ?", (sid,))
        finished_seasons = [str(row[0]) for row in cur.fetchall()]
        finished_str = ", ".join(finished_seasons) or "нет"

        caption = f"🎬 <b>{name}</b>\n📅 Выход: {airing_str}\n👁 Просмотрено: {watched}\n🏁 Сезоны: {finished_str}"

        markup = series_keyboard(sid, completed)

        if poster:
            await message.answer_photo(photo=poster, caption=caption, parse_mode="HTML", reply_markup=markup)
        else:
            await message.answer(caption, parse_mode="HTML", reply_markup=markup)

# ====================== ОТМЕТКА ПРОСМОТРЕННЫХ ЭПИЗОДОВ ======================
@dp.callback_query(F.data.startswith("mark_episodes_"))
async def start_mark_episodes(callback: types.CallbackQuery, state: FSMContext):
    try:
        series_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("❌ Ошибка ID сериала", show_alert=True)
        return

    await state.update_data(series_id=series_id)

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT episodes_per_season FROM series WHERE id = ?", (series_id,))
        result = cur.fetchone()
        episodes_count = result[0] if result and result[0] else 30

    await state.update_data(total_episodes=episodes_count, current_season=1)
    await state.set_state(BotStates.mark_multiple_episodes)

    text = f"🎬 Выберите просмотренные эпизоды сезона 1:"

    try:
        await callback.message.edit_text(
            text=text,
            reply_markup=episodes_keyboard(series_id, 1, episodes_count),
            parse_mode="HTML"
        )
    except Exception:
        # Если edit_text не сработал (например, было фото)
        await callback.message.answer(
            text=text,
            reply_markup=episodes_keyboard(series_id, 1, episodes_count),
            parse_mode="HTML"
        )
        try:
            await callback.message.delete()  # удаляем старое сообщение
        except:
            pass

    await callback.answer("Выберите эпизоды 👇")


@dp.callback_query(F.data.startswith("toggle_ep_"))
async def toggle_episode(callback: types.CallbackQuery, state: FSMContext):
    try:
        _, series_id_str, season_str, ep_str = callback.data.split("_")
        series_id = int(series_id_str)
        season = int(season_str)
        episode = int(ep_str)
    except:
        await callback.answer("❌ Ошибка данных")
        return

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO watched_episodes (series_id, season, episode) VALUES (?, ?, ?)",
            (series_id, season, episode)
        )
        if cur.rowcount == 0:  # уже было — удаляем
            cur.execute(
                "DELETE FROM watched_episodes WHERE series_id = ? AND season = ? AND episode = ?",
                (series_id, season, episode)
            )
        conn.commit()

    data = await state.get_data()
    total = data.get("total_episodes", 30)
    current_season = data.get("current_season", 1)

    try:
        await callback.message.edit_reply_markup(
            reply_markup=episodes_keyboard(series_id, current_season, total)
        )
    except:
        pass  # если не удалось обновить — просто продолжаем

    await callback.answer(f"✅ S{season}E{episode}")


@dp.callback_query(F.data.startswith("finish_marking_"))
async def finish_marking(callback: types.CallbackQuery, state: FSMContext):
    series_id = int(callback.data.split("_")[-1])
    watched_count = get_watched_count(series_id)

    await callback.answer(f"✅ Готово! Просмотрено: {watched_count} эпизодов", show_alert=True)

    await state.clear()

    # Возвращаем обновлённое меню сериала
    await show_series_menu(callback.message, series_id)


@dp.callback_query(F.data == "cancel_marking")
async def cancel_marking(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.delete()
    except:
        pass
    await callback.answer("Отменено")


# ====================== ПОКАЗ МЕНЮ СЕРИАЛА ======================
async def show_series_menu(message: types.Message, series_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT name, poster_file_id, airing_days, completed, episodes_per_season 
            FROM series WHERE id = ?
        """, (series_id,))
        row = cur.fetchone()
        if not row:
            await message.answer("Сериал не найден.")
            return

        name, poster, airing_days, completed, eps = row
        days_map = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        airing_str = ', '.join(days_map[int(d)] for d in airing_days.split(',')) if airing_days else "—"
        watched = get_watched_count(series_id)

        cur.execute("SELECT season FROM finished_seasons WHERE series_id = ?", (series_id,))
        finished_str = ", ".join(str(r[0]) for r in cur.fetchall()) or "нет"

        caption = f"🎬 <b>{name}</b>\n📅 Выход: {airing_str}\n👁 Просмотрено: {watched}\n🏁 Сезоны: {finished_str}"

    markup = series_keyboard(series_id, completed)

    try:
        await message.edit_text(caption, parse_mode="HTML", reply_markup=markup)
    except Exception:
        # Если не получилось отредактировать — отправляем заново
        if poster:
            await message.answer_photo(photo=poster, caption=caption, parse_mode="HTML", reply_markup=markup)
        else:
            await message.answer(caption, parse_mode="HTML", reply_markup=markup)

# ====================== ЗАПУСК БОТА ======================
async def main():
    init_db()
    print("🤖 Бот запущен!")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
