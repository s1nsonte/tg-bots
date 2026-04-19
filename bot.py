import asyncio
import logging
import sqlite3
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO)  

import os

TOKEN = os.getenv("BOT_TOKEN")

# ====================== БАЗА ДАННЫХ ======================
def init_db():
    conn = sqlite3.connect('series_bot.db')
    cur = conn.cursor()
    
    cur.execute('''CREATE TABLE IF NOT EXISTS series (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT,
        poster_file_id TEXT,
        airing_day INTEGER
    )''')
    
    cur.execute('''CREATE TABLE IF NOT EXISTS watched_episodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        series_id INTEGER,
        season INTEGER,
        episode INTEGER
    )''')
    
    cur.execute('''CREATE TABLE IF NOT EXISTS finished_seasons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        series_id INTEGER,
        season INTEGER
    )''')
    
    conn.commit()
    conn.close()

# ====================== СОСТОЯНИЯ ======================
class BotStates(StatesGroup):
    add_name = State()
    add_poster = State()
    input_season_episode = State()
    input_season_finish = State()

# ====================== КЛАВИАТУРЫ ======================
def day_keyboard():
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    buttons = [[types.InlineKeyboardButton(text=day, callback_data=f"day_{i}")] for i in range(7)]
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)

def series_keyboard(series_id: int):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ Отметить эпизод просмотренным", callback_data=f"watch_{series_id}")],
        [types.InlineKeyboardButton(text="🏁 Отметить сезон завершённым", callback_data=f"finish_{series_id}")],
    ])

# ====================== БОТ ======================
bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ====================== КОМАНДЫ ======================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Это бот для трекинга сериалов.\n\n"
        "Команды:\n"
        "/add — добавить новый сериал\n"
        "/my — посмотреть мои сериалы"
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
    await message.answer("Выбери день недели, когда обычно выходит новый эпизод:", reply_markup=day_keyboard())

@dp.message(BotStates.add_poster, Command("skip"))
async def process_poster_skip(message: types.Message, state: FSMContext):
    await state.update_data(poster=None)
    await message.answer("Выбери день недели, когда обычно выходит новый эпизод:", reply_markup=day_keyboard())

@dp.callback_query(lambda c: c.data.startswith("day_"))
async def process_day(callback: types.CallbackQuery, state: FSMContext):
    day = int(callback.data.split("_")[1])
    data = await state.get_data()
    user_id = callback.from_user.id
    name = data.get('name')
    poster = data.get('poster')

    if not name:
        await callback.answer("Ошибка, начни заново /add")
        return

    conn = sqlite3.connect('series_bot.db')
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO series (user_id, name, poster_file_id, airing_day) VALUES (?, ?, ?, ?)",
        (user_id, name, poster, day)
    )
    conn.commit()
    conn.close()

    await state.clear()
    await callback.message.edit_text(f"✅ Сериал «{name}» успешно добавлен!")
    await callback.answer()

@dp.message(Command("my"))
async def cmd_my(message: types.Message):
    user_id = message.from_user.id
    conn = sqlite3.connect('series_bot.db')
    cur = conn.cursor()
    cur.execute("SELECT id, name, poster_file_id, airing_day FROM series WHERE user_id = ?", (user_id,))
    series_list = cur.fetchall()
    
    if not series_list:
        await message.answer("У тебя пока нет сериалов. Добавь первый через /add")
        conn.close()
        return
    
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    
    for ser in series_list:
        sid, name, poster, day = ser
        day_name = days[day]
        
        cur.execute("SELECT COUNT(*) FROM watched_episodes WHERE series_id = ?", (sid,))
        watched = cur.fetchone()[0]
        
        cur.execute("SELECT season FROM finished_seasons WHERE series_id = ?", (sid,))
        finished = [str(row[0]) for row in cur.fetchall()]
        finished_str = ", ".join(finished) if finished else "нет"
        
        caption = (
            f"🎬 <b>{name}</b>\n"
            f"📅 Новый эпизод: {day_name}\n"
            f"👁 Просмотрено эпизодов: {watched}\n"
            f"🏁 Завершённые сезоны: {finished_str}"
        )
        
        if poster:
            await message.answer_photo(photo=poster, caption=caption, parse_mode="HTML", reply_markup=series_keyboard(sid))
        else:
            await message.answer(caption, parse_mode="HTML", reply_markup=series_keyboard(sid))
    
    conn.close()

# ====================== ОТМЕТКА ЭПИЗОДА И СЕЗОНА ======================
@dp.callback_query(lambda c: c.data.startswith("watch_"))
async def cb_watch(callback: types.CallbackQuery, state: FSMContext):
    series_id = int(callback.data.split("_")[1])
    await state.update_data(series_id=series_id)
    await state.set_state(BotStates.input_season_episode)
    await callback.message.answer("Напиши номер сезона и эпизода через пробел\nПример: <code>2 10</code>", parse_mode="HTML")
    await callback.answer()

@dp.message(BotStates.input_season_episode)
async def process_watch_episode(message: types.Message, state: FSMContext):
    try:
        season, episode = map(int, message.text.strip().split())
    except:
        await message.answer("❌ Неверный формат! Пример: 2 10")
        return
    
    data = await state.get_data()
    series_id = data['series_id']
    
    conn = sqlite3.connect('series_bot.db')
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO watched_episodes (series_id, season, episode) VALUES (?, ?, ?)",
                (series_id, season, episode))
    conn.commit()
    conn.close()
    
    await message.answer(f"✅ S{season}E{episode} отмечен как просмотренный!")
    await state.clear()

@dp.callback_query(lambda c: c.data.startswith("finish_"))
async def cb_finish(callback: types.CallbackQuery, state: FSMContext):
    series_id = int(callback.data.split("_")[1])
    await state.update_data(series_id=series_id)
    await state.set_state(BotStates.input_season_finish)
    await callback.message.answer("Напиши номер завершённого сезона (только цифру):")
    await callback.answer()

@dp.message(BotStates.input_season_finish)
async def process_finish_season(message: types.Message, state: FSMContext):
    try:
        season = int(message.text.strip())
    except:
        await message.answer("❌ Введи только число!")
        return
    
    data = await state.get_data()
    series_id = data['series_id']
    
    conn = sqlite3.connect('series_bot.db')
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO finished_seasons (series_id, season) VALUES (?, ?)",
                (series_id, season))
    conn.commit()
    conn.close()
    
    await message.answer(f"🏁 Сезон {season} отмечен как завершённый!")
    await state.clear()

# ====================== ЗАПУСК ======================
async def main():
    init_db()
    print("🤖 Бот запущен...")
    
    # Увеличенные таймауты специально для Windows
    await dp.start_polling(
        bot,
        timeout=180,                    # основной таймаут
        relax=1.0,
        allowed_updates=dp.resolve_used_update_types()
    )

if __name__ == "__main__":
    asyncio.run(main())
