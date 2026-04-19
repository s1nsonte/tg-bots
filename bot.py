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
      