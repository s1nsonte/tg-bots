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

# ====================== ПУТЬ К БАЗЕ ДАННЫХ ДЛЯ RAILWAY ======================
DATA_DIR = "/data"                    # Volume, который ты прикрепил
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "series_bot.db")

print(f"✅ База данных хранится в Volume: {DB_PATH}")


# ====================== БАЗА ДАННЫХ ======================
def init_db():
    """Инициализация базы данных"""
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
def day_keyboard(selected_days: list = None):
    if selected_days is None:
        selected_days = []
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    buttons = []
    for i, day in enumerate(days):
        checked = "🟢" if str(i) in selected_days else "🔳"
        buttons.append([types.InlineKeyboardButton(
            text=f"{checked} {day}",
            callback_data=f"day_{i}"
        )])
    buttons.append([types.InlineKeyboardButton(text="Готово ✅", callback_data="done_days")])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


def episodes_keyboard(season_number: int, total_episodes: int):
    rows = [[types.InlineKeyboardButton(text=str(ep), callback_data=f"ep_{season_number}_{ep}")]
            for ep in range(1, total_episodes + 1)]
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
        "Команды:\n"
        "/add — добавить новый сериал\n"
        "/my  — посмотреть мои сериалы\n"
        "/cancel — отменить текущее действие"
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
    await message.answer("🖼 Отправь постер (фото) или напиши /skip, чтобы пропустить")


@dp.message(BotStates.add_poster, F.photo)
async def process_poster_photo(message: types.Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    await state.update_data(poster=file_id)
    await state.set_state(BotStates.choose_days)
    await message.answer("Выбери дни выхода новых эпизодов:", reply_markup=day_keyboard())


@dp.message(BotStates.add_poster, Command("skip"))
async def process_poster_skip(message: types.Message, state: FSMContext):
    await state.update_data(poster=None)
    await state.set_state(BotStates.choose_days)
    await message.answer("Выбери дни выхода новых эпизодов:", reply_markup=day_keyboard())


@dp.callback_query(F.data.startswith("day_"))
async def select_day(callback: types.CallbackQuery, state: FSMContext):
    day = callback.data.split("_")[1]
    data = await state.get_data()
    selected_days = data.get("selected_days", [])

    if day in selected_days:
        selected_days.remove(day)
    else:
        selected_days.append(day)

    await state.update_data(selected_days=selected_days)
    await callback.message.edit_reply_markup(reply_markup=day_keyboard(selected_days))
    await callback.answer()


@dp.callback_query(F.data == "done_days")
async def confirm_days(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_days = data.get("selected_days", [])

    if not selected_days:
        await callback.answer("Выбери хотя бы один день!", show_alert=True)
        return

    await state.set_state(BotStates.enter_episodes_count)
    days_str = ' '.join(['Пн','Вт','Ср','Чт','Пт','Сб','Вс'][int(d)] for d in selected_days)
    
    await callback.message.edit_text(
        f"Название: {data.get('name')}\n"
        f"Дни выхода: {days_str}\n\n"
        "Введи количество серий в сезоне (если знаешь)\n"
        "или напиши /skip, чтобы пропустить"
    )
    await callback.answer()


@dp.message(BotStates.enter_episodes_count, ~Command("skip"))
async def process_episodes_count(message: types.Message, state: FSMContext):
    try:
        count = int(message.text.strip())
        if count <= 0:
            raise ValueError
        await state.update_data(episodes_count=count)
    except ValueError:
        await message.answer("❌ Введи положительное целое число!")
        return

    await show_confirmation(message, state)


@dp.message(BotStates.enter_episodes_count, Command("skip"))
async def skip_episodes_count(message: types.Message, state: FSMContext):
    await show_confirmation(message, state)


async def show_confirmation(message: types.Message, state: FSMContext):
    data = await state.get_data()
    days_map = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    selected = data.get("selected_days", [])
    days_str = ' '.join(days_map[int(d)] for d in selected)

    text = f"Название: {data.get('name')}\nДни выхода: {days_str}\n"
    if data.get("episodes_count"):
        text += f"Серий в сезоне: {data['episodes_count']}\n"
    text += "\nВсё верно?"

    await message.answer(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="Да, сохранить 💾", callback_data="save_series")],
            [types.InlineKeyboardButton(text="Нет, начать заново", callback_data="restart_add")]
        ])
    )


@dp.callback_query(F.data == "save_series")
async def save_series(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = callback.from_user.id
    name = data.get('name')

    if not name:
        await callback.answer("Ошибка: название сериала не найдено", show_alert=True)
        return

    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO series 
                   (user_id, name, poster_file_id, airing_days, episodes_per_season)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, name, data.get('poster'), ','.join(data.get('selected_days', [])), data.get('episodes_count'))
            )
            conn.commit()

        await state.clear()
        await callback.message.edit_text(f"✅ Сериал «{name}» успешно добавлен!")
        await callback.answer("Сохранено ✓")

    except Exception as e:
        logging.error(f"Ошибка сохранения: {e}")
        await callback.answer("❌ Ошибка при сохранении в базу", show_alert=True)


@dp.callback_query(F.data == "restart_add")
async def restart_add(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.choose_days)
    await callback.message.edit_text(
        "Выбери дни выхода новых эпизодов:",
        reply_markup=day_keyboard()
    )
    await callback.answer()


# ====================== МОИ СЕРИАЛЫ ======================
@dp.message(Command("my"))
async def cmd_my(message: types.Message):
    user_id = message.from_user.id

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, name, poster_file_id, airing_days, completed, episodes_per_season 
               FROM series WHERE user_id = ?""", 
            (user_id,)
        )
        series_list = cur.fetchall()

    if not series_list:
        await message.answer("У тебя пока нет сериалов. Добавь первый через /add")
        return

    days_map = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    for ser in series_list:
        sid, name, poster, airing_days, completed, eps = ser
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


# ====================== ЗАПУСК ======================
async def main():
    init_db()
    print("🤖 Бот для трекинга сериалов успешно запущен...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())