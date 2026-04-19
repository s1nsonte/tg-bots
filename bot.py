import asyncio
import logging
import sqlite3
import os

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ====================== НАСТРОЙКИ ======================
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN is missing or empty!")

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# ====================== БАЗА ДАННЫХ ======================
def init_db():
    """Инициализация базы данных"""
    with sqlite3.connect('series_bot.db') as conn:
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


# ====================== СОСТОЯНИЯ ======================
class BotStates(StatesGroup):
    add_name = State()
    add_poster = State()
    choose_days = State()
    enter_episodes_count = State()
    confirm_save = State()
    input_season_episode = State()
    input_season_finish = State()
    mark_multiple_episodes = State()
    mark_multiple_season = State()   # новое состояние, если episodes_per_season не задан


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
        "👋 Привет! Это бот для трекинга просмотра сериалов.\n\n"
        "Доступные команды:\n"
        "/add — добавить новый сериал\n"
        "/my  — посмотреть мои сериалы"
    )


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
    await callback.message.edit_text(
        f"Выбраны дни: {' '.join(['Пн','Вт','Ср','Чт','Пт','Сб','Вс'][int(d)] for d in selected_days)}\n\n"
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
    except ValueError:
        await message.answer("Пожалуйста, введи положительное целое число!")
        return

    await state.update_data(episodes_count=count)
    await show_confirmation(message, state)


@dp.message(BotStates.enter_episodes_count, Command("skip"))
async def skip_episodes_count(message: types.Message, state: FSMContext):
    await show_confirmation(message, state)


async def show_confirmation(message: types.Message, state: FSMContext):
    data = await state.get_data()
    days_map = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    selected_days = data.get("selected_days", [])
    days_str = ' '.join(days_map[int(d)] for d in selected_days)

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

    with sqlite3.connect('series_bot.db') as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO series 
               (user_id, name, poster_file_id, airing_days, episodes_per_season)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id,
             data.get('name'),
             data.get('poster'),
             ','.join(data.get('selected_days', [])),
             data.get('episodes_count'))
        )

    await state.clear()
    await callback.message.edit_text(f"✅ Сериал «{data.get('name')}» успешно добавлен!")
    await callback.answer()


@dp.callback_query(F.data == "restart_add")
async def restart_add(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.choose_days)
    await callback.message.edit_text("Выбери дни выхода новых эпизодов:", reply_markup=day_keyboard())
    await callback.answer()


# ====================== МОИ СЕРИАЛЫ ======================
@dp.message(Command("my"))
async def cmd_my(message: types.Message):
    user_id = message.from_user.id

    with sqlite3.connect('series_bot.db') as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, poster_file_id, airing_days, completed, episodes_per_season "
            "FROM series WHERE user_id = ?",
            (user_id,)
        )
        series_list = cur.fetchall()

    if not series_list:
        await message.answer("У тебя пока нет добавленных сериалов.\nДобавь первый через /add")
        return

    days_map = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    for series in series_list:
        sid, name, poster, airing_days, completed, eps_per_season = series

        airing_str = ', '.join(days_map[int(d)] for d in airing_days.split(',')) if airing_days else "—"

        # Количество просмотренных эпизодов
        cur.execute("SELECT COUNT(*) FROM watched_episodes WHERE series_id = ?", (sid,))
        watched_count = cur.fetchone()[0]

        # Завершённые сезоны
        cur.execute("SELECT season FROM finished_seasons WHERE series_id = ?", (sid,))
        finished = [str(row[0]) for row in cur.fetchall()]
        finished_str = ", ".join(finished) if finished else "нет"

        caption = (
            f"🎬 <b>{name}</b>\n"
            f"📅 Выход: {airing_str}\n"
            f"👁 Просмотрено: {watched_count}\n"
            f"🏁 Завершённые сезоны: {finished_str}"
        )

        if poster:
            await message.answer_photo(
                photo=poster,
                caption=caption,
                parse_mode="HTML",
                reply_markup=series_keyboard(sid, completed)
            )
        else:
            await message.answer(
                caption,
                parse_mode="HTML",
                reply_markup=series_keyboard(sid, completed)
            )


# ====================== УПРАВЛЕНИЕ СЕРИАЛОМ ======================
@dp.callback_query(F.data.startswith("complete_"))
async def complete_series(callback: types.CallbackQuery):
    series_id = int(callback.data.split("_")[1])

    with sqlite3.connect('series_bot.db') as conn:
        conn.execute("UPDATE series SET completed = TRUE WHERE id = ?", (series_id,))

    await callback.message.edit_caption(
        caption="✅ Сериал отмечен как завершённый",
        reply_markup=series_keyboard(series_id, True)
    )
    await callback.answer("Сериал завершён!")


@dp.callback_query(F.data.startswith("delete_"))
async def delete_series(callback: types.CallbackQuery):
    series_id = int(callback.data.split("_")[1])

    with sqlite3.connect('series_bot.db') as conn:
        conn.execute("DELETE FROM series WHERE id = ?", (series_id,))
        conn.execute("DELETE FROM watched_episodes WHERE series_id = ?", (series_id,))
        conn.execute("DELETE FROM finished_seasons WHERE series_id = ?", (series_id,))

    await callback.message.delete()
    await callback.answer("Сериал удалён")


@dp.callback_query(F.data.startswith("watch_"))
async def start_watch_episode(callback: types.CallbackQuery, state: FSMContext):
    series_id = int(callback.data.split("_")[1])
    await state.update_data(series_id=series_id)
    await state.set_state(BotStates.input_season_episode)
    await callback.message.answer("Напиши номер сезона и эпизода через пробел\nПример: <code>2 10</code>", parse_mode="HTML")
    await callback.answer()


@dp.message(BotStates.input_season_episode)
async def process_watch_episode(message: types.Message, state: FSMContext):
    try:
        season, episode = map(int, message.text.strip().split())
    except Exception:
        await message.answer("❌ Неверный формат! Пример: 2 10")
        return

    data = await state.get_data()
    series_id = data["series_id"]

    with sqlite3.connect('series_bot.db') as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watched_episodes (series_id, season, episode) VALUES (?, ?, ?)",
            (series_id, season, episode)
        )

    await message.answer(f"✅ S{season}E{episode} отмечен как просмотренный!")
    await state.clear()


@dp.callback_query(F.data.startswith("finish_"))
async def start_finish_season(callback: types.CallbackQuery, state: FSMContext):
    series_id = int(callback.data.split("_")[1])
    await state.update_data(series_id=series_id)
    await state.set_state(BotStates.input_season_finish)
    await callback.message.answer("Напиши номер завершённого сезона (только цифру):")
    await callback.answer()


@dp.message(BotStates.input_season_finish)
async def process_finish_season(message: types.Message, state: FSMContext):
    try:
        season = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введи только число!")
        return

    data = await state.get_data()
    series_id = data["series_id"]

    with sqlite3.connect('series_bot.db') as conn:
        conn.execute(
            "INSERT OR IGNORE INTO finished_seasons (series_id, season) VALUES (?, ?)",
            (series_id, season)
        )

    await message.answer(f"🏁 Сезон {season} отмечен как завершённый!")
    await state.clear()


# ====================== МАССОВАЯ ОТМЕТКА СЕРИЙ ======================
@dp.callback_query(F.data.startswith("mark_episodes_"))
async def start_mark_episodes(callback: types.CallbackQuery, state: FSMContext):
    series_id = int(callback.data.split("_")[1])
    await state.update_data(series_id=series_id)

    with sqlite3.connect('series_bot.db') as conn:
        cur = conn.cursor()
        cur.execute("SELECT episodes_per_season FROM series WHERE id = ?", (series_id,))
        episodes_count = cur.fetchone()[0]

    if episodes_count:
        await state.update_data(total_episodes=episodes_count)
        await state.set_state(BotStates.mark_multiple_episodes)
        await callback.message.answer(
            f"Выбери серии для отметки (сезон 1):",
            reply_markup=episodes_keyboard(1, episodes_count)
        )
    else:
        await state.set_state(BotStates.mark_multiple_season)
        await callback.message.answer("Укажи номер сезона для отметки серий:")
    await callback.answer()


@dp.message(BotStates.mark_multiple_season)
async def process_mark_season(message: types.Message, state: FSMContext):
    try:
        season = int(message.text.strip())
    except ValueError:
        await message.answer("Введи только число!")
        return

    data = await state.get_data()
    series_id = data["series_id"]

    # Здесь можно добавить запрос количества серий, но для простоты просим выбрать в клавиатуре позже
    await state.update_data(current_season=season, total_episodes=999)  # заглушка
    await state.set_state(BotStates.mark_multiple_episodes)
    await message.answer(f"Выбери серии сезона {season} для отметки (пока без ограничения):",
                         reply_markup=episodes_keyboard(season, 30))  # можно увеличить или сделать динамически


@dp.callback_query(F.data.startswith("ep_"))
async def mark_single_episode(callback: types.CallbackQuery, state: FSMContext):
    _, season_str, episode_str = callback.data.split("_")
    season = int(season_str)
    episode = int(episode_str)

    data = await state.get_data()
    series_id = data["series_id"]

    with sqlite3.connect('series_bot.db') as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watched_episodes (series_id, season, episode) VALUES (?, ?, ?)",
            (series_id, season, episode)
        )

    # Обновляем клавиатуру (если возможно)
    try:
        total = data.get("total_episodes", 30)
        await callback.message.edit_reply_markup(
            reply_markup=episodes_keyboard(season, total)
        )
    except Exception:
        pass  # если сообщение нельзя отредактировать

    await callback.answer(f"S{season}E{episode} просмотрено")


@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.answer("Возвращаемся в меню")


# ====================== ЗАПУСК ======================
async def main():
    init_db()
    print("🤖 Бот для трекинга сериалов запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())