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

# ====================== ПУТЬ К БАЗЕ ДАННЫХ ======================
DATA_DIR = "/data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "series_bot.db")

print(f"✅ База данных хранится в: {DB_PATH}")


# ====================== БАЗА ДАННЫХ ======================
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
    print("✅ База данных успешно инициализирована")


# ====================== СОСТОЯНИЯ ======================
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


# ====================== ОБРАБОТЧИКИ ======================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Это бот для трекинга сериалов.\n\n"
        "/add — добавить сериал\n"
        "/my — мои сериалы\n"
        "/cancel — отменить действие"
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Действие отменено.")


@dp.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.add_name)
    await message.answer("📝 Введи название сериала:")


@dp.message(BotStates.add_name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(BotStates.add_poster)
    await message.answer("🖼 Отправь постер или напиши /skip")


@dp.message(BotStates.add_poster, F.photo)
async def process_poster(message: types.Message, state: FSMContext):
    await state.update_data(poster=message.photo[-1].file_id)
    await state.set_state(BotStates.choose_days)
    await message.answer("Выбери дни выхода:", reply_markup=day_keyboard())


@dp.message(BotStates.add_poster, Command("skip"))
async def skip_poster(message: types.Message, state: FSMContext):
    await state.update_data(poster=None)
    await state.set_state(BotStates.choose_days)
    await message.answer("Выбери дни выхода:", reply_markup=day_keyboard())


@dp.callback_query(F.data.startswith("day_"))
async def select_day(callback: types.CallbackQuery, state: FSMContext):
    day = callback.data.split("_")[1]
    data = await state.get_data()
    selected = data.get("selected_days", [])

    if day in selected:
        selected.remove(day)
    else:
        selected.append(day)

    await state.update_data(selected_days=selected)
    await callback.message.edit_reply_markup(reply_markup=day_keyboard(selected))
    await callback.answer()


@dp.callback_query(F.data == "done_days")
async def done_days(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_days", [])

    if not selected:
        await callback.answer("Выбери хотя бы один день!", show_alert=True)
        return

    await state.set_state(BotStates.enter_episodes_count)
    days_str = ' '.join(['Пн','Вт','Ср','Чт','Пт','Сб','Вс'][int(d)] for d in selected)
    await callback.message.edit_text(
        f"Название: {data.get('name')}\nДни выхода: {days_str}\n\n"
        "Количество серий в сезоне (или /skip):"
    )
    await callback.answer()


@dp.message(BotStates.enter_episodes_count, ~Command("skip"))
async def process_episodes(message: types.Message, state: FSMContext):
    try:
        count = int(message.text.strip())
        if count <= 0: raise ValueError
        await state.update_data(episodes_count=count)
    except:
        await message.answer("❌ Введи положительное число!")
        return
    await show_confirmation(message, state)


@dp.message(BotStates.enter_episodes_count, Command("skip"))
async def skip_episodes(message: types.Message, state: FSMContext):
    await show_confirmation(message, state)


async def show_confirmation(message: types.Message, state: FSMContext):
    data = await state.get_data()
    days_str = ' '.join(['Пн','Вт','Ср','Чт','Пт','Сб','Вс'][int(d)] for d in data.get("selected_days", []))

    text = f"Название: {data.get('name')}\nДни выхода: {days_str}\n"
    if data.get("episodes_count"):
        text += f"Серий в сезоне: {data['episodes_count']}\n"
    text += "\nВсё верно?"

    await message.answer(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Да, сохранить 💾", callback_data="save_series")],
        [types.InlineKeyboardButton(text="Нет, начать заново", callback_data="restart_add")]
    ]))


@dp.callback_query(F.data == "save_series")
async def save_series(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = callback.from_user.id
    name = data.get('name')

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """INSERT INTO series (user_id, name, poster_file_id, airing_days, episodes_per_season)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, name, data.get('poster'), ','.join(data.get('selected_days', [])), data.get('episodes_count'))
            )
        await state.clear()
        await callback.message.edit_text(f"✅ Сериал «{name}» добавлен!")
        await callback.answer("Сохранено")
    except Exception as e:
        logging.error(f"Save error: {e}")
        await callback.answer("❌ Ошибка сохранения", show_alert=True)


@dp.callback_query(F.data == "restart_add")
async def restart_add(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.choose_days)
    await callback.message.edit_text("Выбери дни выхода:", reply_markup=day_keyboard())
    await callback.answer()


# ====================== МОИ СЕРИАЛЫ ======================
@dp.message(Command("my"))
async def cmd_my(message: types.Message):
    user_id = message.from_user.id

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, poster_file_id, airing_days, completed, episodes_per_season "
            "FROM series WHERE user_id = ?", (user_id,)
        )
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
        finished = [str(row[0]) for row in cur.fetchall()]
        finished_str = ", ".join(finished) if finished else "нет"

        caption = (
            f"🎬 <b>{name}</b>\n"
            f"📅 Выход: {airing_str}\n"
            f"👁 Просмотрено: {watched}\n"
            f"🏁 Завершённые сезоны: {finished_str}"
        )

        if poster:
            await message.answer_photo(photo=poster, caption=caption, parse_mode="HTML",
                                       reply_markup=series_keyboard(sid, completed))
        else:
            await message.answer(caption, parse_mode="HTML", reply_markup=series_keyboard(sid, completed))


# ====================== КНОПКИ УПРАВЛЕНИЯ ======================
@dp.callback_query(F.data.startswith("watch_"))
async def cb_watch(callback: types.CallbackQuery, state: FSMContext):
    series_id = int(callback.data.split("_")[1])
    await state.update_data(series_id=series_id)
    await state.set_state(BotStates.input_season_episode)
    await callback.message.answer("Напиши сезон и эпизод через пробел\nПример: <code>2 10</code>", parse_mode="HTML")
    await callback.answer()


@dp.message(BotStates.input_season_episode)
async def process_watch(message: types.Message, state: FSMContext):
    try:
        season, episode = map(int, message.text.strip().split())
    except:
        await message.answer("❌ Формат: 2 10")
        return

    data = await state.get_data()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO watched_episodes VALUES (NULL, ?, ?, ?)",
                     (data['series_id'], season, episode))

    await message.answer(f"✅ S{season}E{episode} просмотрен!")
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
        conn.execute("INSERT OR IGNORE INTO finished_seasons VALUES (NULL, ?, ?)",
                     (data['series_id'], season))

    await message.answer(f"🏁 Сезон {season} завершён!")
    await state.clear()


@dp.callback_query(F.data.startswith("complete_"))
async def cb_complete(callback: types.CallbackQuery):
    series_id = int(callback.data.split("_")[1])
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE series SET completed = 1 WHERE id = ?", (series_id,))

    await callback.message.edit_caption("✅ Сериал завершён", reply_markup=series_keyboard(series_id, True))
    await callback.answer()


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
async def cb_mark_start(callback: types.CallbackQuery, state: FSMContext):
    series_id = int(callback.data.split("_")[1])
    await state.update_data(series_id=series_id)

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT episodes_per_season FROM series WHERE id = ?", (series_id,))
        eps = cur.fetchone()[0]

    if eps:
        await state.set_state(BotStates.mark_multiple_episodes)
        await callback.message.answer("Выбери серии сезона 1:", reply_markup=episodes_keyboard(1, eps))
    else:
        await state.set_state(BotStates.mark_multiple_season)
        await callback.message.answer("Напиши номер сезона:")
    await callback.answer()


# ====================== ЗАПУСК ======================
async def main():
    init_db()
    print("🤖 Бот успешно запущен!")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
