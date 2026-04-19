import asyncio
import logging
import sqlite3
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import os

# Настройка логгирования
logging.basicConfig(level=logging.INFO)

# Получение токена из переменных окружения
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN is missing or empty!")

# Создание экземпляра бота и диспетчера
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
                name TEXT,
                poster_file_id TEXT,
                airing_days TEXT,
                episodes_per_season INTEGER NULL,  -- новая колонка для количества серий в сезоне
                completed BOOLEAN DEFAULT FALSE
            );
            
            CREATE TABLE IF NOT EXISTS watched_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER,
                season INTEGER,
                episode INTEGER
            );
            
            CREATE TABLE IF NOT EXISTS finished_seasons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER,
                season INTEGER
            );
        ''')

# ====================== СОСТОЯНИЯ ======================
class BotStates(StatesGroup):
    """Состояния для FSM"""
    add_name = State()                  # ввод названия сериала
    add_poster = State()               # отправка постера
    choose_days = State()               # выбор дней выхода
    confirm_days = State()              # подтверждение выбранных дней
    enter_episodes_count = State()      # ввод количества серий в сезоне (по желанию)
    input_season_episode = State()      # ввод номера серии и эпизода
    input_season_finish = State()       # ввод завершённого сезона
    mark_multiple_episodes = State()    # отметка нескольких серий

# ====================== КЛАВИАТУРЫ ======================
def day_keyboard(selected_days=[]):
    """Клавиатура выбора дней недели с чекбоксами"""
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    buttons = []
    for i, day in enumerate(days):
        checked = "🟢" if str(i) in selected_days else "🔳"
        button = types.InlineKeyboardButton(text=f"{checked} {day}", callback_data=f"day_{i}")
        buttons.append([button])
    buttons.append([types.InlineKeyboardButton(text="Готово ✅", callback_data="done")])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)

def episodes_keyboard(season_number, total_episodes):
    """Клавиатура выбора серий для отметки"""
    rows = []
    for ep in range(1, total_episodes + 1):
        button = types.InlineKeyboardButton(text=str(ep), callback_data=f"ep_{season_number}_{ep}")
        rows.append([button])
    rows.append([types.InlineKeyboardButton(text="Назад ↩️", callback_data="back_to_menu")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)

def series_keyboard(series_id: int, completed=False):
    """Клавиатура для управления сериалом"""
    keyboard = [
        [types.InlineKeyboardButton(text="✅ Отметить эпизод просмотренным", callback_data=f"watch_{series_id}")],
        [types.InlineKeyboardButton(text="🏁 Отметить сезон завершённым", callback_data=f"finish_{series_id}")]
    ]
    
    # Добавляем кнопку завершения сериала
    if not completed:
        keyboard.append([types.InlineKeyboardButton(text="Завершить сериал 🆕", callback_data=f"complete_{series_id}")])
    
    # Добавляем кнопку удаления сериала
    keyboard.append([types.InlineKeyboardButton(text="Удалить сериал 🗑", callback_data=f"delete_{series_id}")])
    
    # Добавляем кнопку для массовой отметки серий
    keyboard.append([types.InlineKeyboardButton(text="Отметить серию(-ии)", callback_data=f"mark_episodes_{series_id}")])
    
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)

# ====================== ОБРАБОТЧИКИ КОМАНД ======================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Приветственное сообщение"""
    await message.answer(
        "👋 Привет! Это бот для трекинга сериалов.\n\n"
        "Команды:\n"
        "/add — добавить новый сериал\n"
        "/my — посмотреть мои сериалы"
    )

@dp.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext):
    """Начало процесса добавления сериала"""
    await state.set_state(BotStates.add_name)
    await message.answer("📝 Введи название сериала:")

@dp.message(BotStates.add_name)
async def process_name(message: types.Message, state: FSMContext):
    """Обработка введённого названия сериала"""
    await state.update_data(name=message.text.strip())
    await state.set_state(BotStates.add_poster)
    await message.answer("🖼 Отправь постер (фото) или напиши /skip, чтобы пропустить")

@dp.message(BotStates.add_poster, F.photo)
async def process_poster_photo(message: types.Message, state: FSMContext):
    """Обработка отправки фотографии-постера"""
    file_id = message.photo[-1].file_id
    await state.update_data(poster=file_id)
    await state.set_state(BotStates.choose_days)
    await message.answer("Выбери дни недели, когда обычно выходят новые эпизоды:", reply_markup=day_keyboard())

@dp.message(BotStates.add_poster, Command("skip"))
async def process_poster_skip(message: types.Message, state: FSMContext):
    """Обработка пропуска отправки постера"""
    await state.update_data(poster=None)
    await state.set_state(BotStates.choose_days)
    await message.answer("Выбери дни недели, когда обычно выходят новые эпизоды:", reply_markup=day_keyboard())

@dp.callback_query(F.data.startswith("day_"))
async def select_day(callback: types.CallbackQuery, state: FSMContext):
    """Выбор дня недели"""
    day = int(callback.data.split("_")[1])
    current_days = await state.get_data().get("selected_days", [])
    
    # Переключаем состояние кнопки (выбран/не выбран)
    if str(day) in current_days:
        current_days.remove(str(day))
    else:
        current_days.append(str(day))
    
    await state.update_data(selected_days=current_days)
    await callback.message.edit_reply_markup(reply_markup=day_keyboard(current_days))
    await callback.answer()

@dp.callback_query(F.data == "done")
async def confirm_days(callback: types.CallbackQuery, state: FSMContext):
    """Подтверждение выбранных дней"""
    selected_days = await state.get_data().get("selected_days", [])
    if not selected_days:
        await callback.answer("Выбери хотя бы один день!")
        return
    
    await state.set_state(BotStates.enter_episodes_count)
    await callback.message.edit_text(
        f"Ты выбрал следующие дни: {' '.join(map(lambda x: ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'][int(x)], selected_days))}\n\n"
        "Введи количество серий в сезоне (если знаешь)\n"
        "(или нажми /skip, чтобы пропустить этот шаг)"
    )
    await callback.answer()

@dp.message(BotStates.enter_episodes_count, ~Command("skip"))
async def process_episodes_count(message: types.Message, state: FSMContext):
    """Обработка ввода количества серий в сезоне"""
    try:
        episodes_count = int(message.text.strip())
        if episodes_count <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи положительное целое число!")
        return
    
    await state.update_data(episodes_count=episodes_count)
    await state.set_state(BotStates.confirm_days)
    await message.answer("Данные сериала готовы к сохранению!\n\n"
                        "Название: {}\n"
                        "Дни выхода: {}\n"
                        "Серий в сезоне: {}\n\n"
                        "Всё верно?".format(await state.get_data().get('name'),
                                            ' '.join(map(lambda x: ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'][int(x)],
                                                     await state.get_data().get('selected_days'))),
                                            await state.get_data().get('episodes_count')),
                       reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                           [types.InlineKeyboardButton(text="Да, сохранить 💾", callback_data="save_days")],
                           [types.InlineKeyboardButton(text="Нет, начать заново ⚪️", callback_data="reselect")]
                       ]))

@dp.message(BotStates.enter_episodes_count, Command("skip"))
async def skip_episodes_count(message: types.Message, state: FSMContext):
    """Обработка пропуска шага с количеством серий"""
    await state.set_state(BotStates.confirm_days)
    await message.answer("Данные сериала готовы к сохранению!\n\n"
                        "Название: {}\n"
                        "Дни выхода: {}\n\n"
                        "Всё верно?".format(await state.get_data().get('name'),
                                            ' '.join(map(lambda x: ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'][int(x)],
                                                     await state.get_data().get('selected_days')))),
                       reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                           [types.InlineKeyboardButton(text="Да, сохранить 💾", callback_data="save_days")],
                           [types.InlineKeyboardButton(text="Нет, начать заново ⚪️", callback_data="reselect")]
                       ]))

@dp.callback_query(F.data == "save_days")
async def save_days(callback: types.CallbackQuery, state: FSMContext):
    """Сохранение выбранных дней"""
    data = await state.get_data()
    user_id = callback.from_user.id
    name = data.get('name')
    poster = data.get('poster')
    selected_days = ','.join(data.get('selected_days'))
    episodes_count = data.get('episodes_count')

    if not name:
        await callback.answer("Ошибка, начни заново /add")
        return

    with sqlite3.connect('series_bot.db') as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO series (user_id, name, poster_file_id, airing_days, episodes_per_season) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, name, poster, selected_days, episodes_count)
        )

    await state.clear()
    await callback.message.edit_text(f"✅ Сериал «{name}» успешно добавлен!")
    await callback.answer()

@dp.callback_query(F.data == "reselect")
async def reselect_days(callback: types.CallbackQuery, state: FSMContext):
    """Переход обратно к выбору дней"""
    await state.set_state(BotStates.choose_days)
    await callback.message.edit_text("Выбери дни недели, когда обычно выходят новые эпизоды:", reply_markup=day_keyboard())
    await callback.answer()

# ====================== МОИ СЕРИАЛЫ (/MY) ======================
@dp.message(Command("my"))
async def cmd_my(message: types.Message):
    """Показ моих сериалов"""
    user_id = message.from_user.id
    with sqlite3.connect('series_bot.db') as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name, poster_file_id, airing_days, completed, episodes_per_season FROM series WHERE user_id = ?", (user_id,))
        series_list = cur.fetchall()

        if not series_list:
            await message.answer("У тебя пока нет сериалов. Добавь первый через /add")
            return

        days_map = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

        for ser in series_list:
            sid, name, poster, airing_days, completed, episodes_per_season = ser
            airing_days_names = ', '.join(map(lambda x: days_map[int(x)], airing_days.split(',')))

            cur.execute("SELECT COUNT(*) FROM watched_episodes WHERE series_id = ?", (sid,))
            watched = cur.fetchone()[0]

            cur.execute("SELECT season FROM finished_seasons WHERE series_id = ?", (sid,))
            finished = [str(row[0]) for row in cur.fetchall()]
            finished_str = ", ".join(finished) if finished else "нет"

            caption = (
                f"🎬 <b>{name}</b>\n"
                f"📅 Новые эпизоды: {airing_days_names}\n"
                f"👁 Просмотрено эпизодов: {watched}\n"
                f"🏁 Завершённые сезоны: {finished_str}"
            )

            if poster:
                await message.answer_photo(photo=poster, caption=caption, parse_mode="HTML", reply_markup=series_keyboard(sid, completed))
            else:
                await message.answer(caption, parse_mode="HTML", reply_markup=series_keyboard(sid, completed))

# ====================== ОТМЕТИТЬ СЕРИАЛ ЗАВЕРШЁННЫМ ======================
@dp.callback_query(lambda c: c.data.startswith("complete_"))
async def complete_series(callback: types.CallbackQuery):
    """Помечаем сериал как завершённый"""
    series_id = int(callback.data.split("_")[1])
    
    with sqlite3.connect('series_bot.db') as conn:
        cur = conn.cursor()
        cur.execute("UPDATE series SET completed = TRUE WHERE id = ?", (series_id,))
    
    await callback.message.edit_caption(
        caption=f"Сериал завершён!",
        reply_markup=series_keyboard(series_id, True)
    )
    await callback.answer("Сериал отмечен как завершённый!")

# ====================== УДАЛИТЬ СЕРИАЛ ИЗ СПИСКА ======================
@dp.callback_query(lambda c: c.data.startswith("delete_"))
async def delete_series(callback: types.CallbackQuery):
    """Удаляем сериал из базы данных"""
    series_id = int(callback.data.split("_")[1])
    
    with sqlite3.connect('series_bot.db') as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM series WHERE id = ?", (series_id,))
        cur.execute("DELETE FROM watched_episodes WHERE series_id = ?", (series_id,))
        cur.execute("DELETE FROM finished_seasons WHERE series_id = ?", (series_id,))
    
    await callback.message.delete()
    await callback.answer("Сериал удалён из списка!")

# ====================== ОТМЕТКА ПРОСМОТРЕННЫХ СЕРИЙ ПО ОДНОЙ ======================
@dp.callback_query(lambda c: c.data.startswith("watch_"))
async def cb_watch(callback: types.CallbackQuery, state: FSMContext):
    """Запуск процесса отметки просмотренного эпизода"""
    series_id = int(callback.data.split("_")[1])
    await state.update_data(series_id=series_id)
    await state.set_state(BotStates.input_season_episode)
    await callback.message.answer("Напиши номер сезона и эпизода через пробел\nПример: <code>2 10</code>", parse_mode="HTML")
    await callback.answer()

@dp.message(BotStates.input_season_episode)
async def process_watch_episode(message: types.Message, state: FSMContext):
    """Обработка ввода номера сезона и эпизода"""
    try:
        season, episode = map(int, message.text.strip().split())
    except Exception:
        await message.answer("❌ Неверный формат! Пример: 2 10")
        return

    data = await state.get_data()
    series_id = data['series_id']

    with sqlite3.connect('series_bot.db') as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO watched_episodes (series_id, season, episode) VALUES (?, ?, ?)",
            (series_id, season, episode)
        )

    await message.answer(f"✅ S{season}E{episode} отмечен как просмотренный!")
    await state.clear()

# ====================== ОТМЕТКА НЕСКОЛЬКИХ СЕРИЙ ЗА РАЗ ======================
@dp.callback_query(lambda c: c.data.startswith("mark_episodes_"))
async def start_mark_episodes(callback: types.CallbackQuery, state: FSMContext):
    """Начало процесса массовой отметки серий"""
    series_id = int(callback.data.split("_")[1])
    await state.update_data(series_id=series_id)
    
    with sqlite3.connect('series_bot.db') as conn:
        cur = conn.cursor()
        cur.execute("SELECT episodes_per_season FROM series WHERE id = ?", (series_id,))
        episodes_count = cur.fetchone()[0]
    
    if episodes_count is None:
        await callback.message.answer("Сначала выбери сезон, а потом серию:")
        await state.set_state(BotStates.mark_multiple_episodes)
        await callback.answer()
    else:
        await state.set_state(BotStates.mark_multiple_episodes)
        await callback.message.answer("Выбери серию(-ии) для отметки:",
                                    reply_markup=episodes_keyboard(1, episodes_count))
        await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("ep_"))
async def mark_episode(callback: types.CallbackQuery, state: FSMContext):
    """Отметка выбранной серии как просмотренной"""
    _, season, episode = callback.data.split("_")
    season = int(season)
    episode = int(episode)
    
    data = await state.get_data()
    series_id = data['series_id']
    
    with sqlite3.connect('series_bot.db') as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO watched_episodes (series_id, season, episode) VALUES (?, ?, ?)",
            (series_id, season, episode)
        )
    
    await callback.message.edit_reply_markup(reply_markup=episodes_keyboard(season, await state.get_data().get('total_episodes')))
    await callback.answer(f"S{season}E{episode} отмечен как просмотренный!")

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    await state.clear()
    await callback.message.delete()
    await callback.answer("Вернулся в основное меню.")

# ====================== ОТМЕТКА ЗАВЁРШЁННОГО СЕЗОНА ======================
@dp.callback_query(lambda c: c.data.startswith("finish_"))
async def cb_finish(callback: types.CallbackQuery, state: FSMContext):
    """Запуск процесса отметки завершённого сезона"""
    series_id = int(callback.data.split("_")[1])
    await state.update_data(series_id=series_id)
    await state.set_state(BotStates.input_season_finish)
    await callback.message.answer("Напиши номер завершённого сезона (только цифру):")
    await callback.answer()

@dp.message(BotStates.input_season_finish)
async def process_finish_season(message: types.Message, state: FSMContext):
    """Обработка ввода завершённого сезона"""
    try:
        season = int(message.text.strip())
    except Exception:
        await message.answer("❌ Введи только число!")
        return

    data = await state.get_data()
    series_id = data['series_id']

    with sqlite3.connect('series_bot.db') as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO finished_seasons (series_id, season) VALUES (?, ?)",
            (series_id, season)
        )

    await message.answer(f"🏁 Сезон {season} отмечен как завершённый!")
    await state.clear()

# ====================== ЗАПУСК БОТА ======================
async def main():
    """Главная функция запуска бота"""
    init_db()  # Инициализация базы данных
    print("🤖 Бот запущен...")
    
    # Запуск поллинга
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())