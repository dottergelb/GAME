import asyncio
import os
from pathlib import Path
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from dotenv import load_dotenv

from database import init_db, save_user_name, get_user_name

load_dotenv(dotenv_path=Path(__file__).with_name(".env"))
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Add it to .env")

bot = Bot(token=TOKEN)
dp = Dispatcher()


class Form(StatesGroup):
    name = State()


@dp.startup()
async def on_startup():
    await init_db()
    print("База данных инициализирована.")


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    name = await get_user_name(message.from_user.id)

    if name:
        await message.answer(f"Welcome to esports {name}!")
    else:
        await message.answer("Hello, enter your game name. Please enter exactly the name you specified in the game.")
        await state.set_state(Form.name)


@dp.message(Form.name)
async def process_name(message: types.Message, state: FSMContext):
    name = message.text.strip()

    if not name or len(name) < 2:
        await message.answer("Please enter your name (at least 2 characters).")
        return

    await save_user_name(message.from_user.id, name)

    # update_data не обязателен, если вы сразу сохраняете в БД
    await state.update_data(name=name)

    await state.clear()

    await message.answer(f"Welcome to esports {name}!")


async def main():
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        print('Bot stopped by user.')
    except Exception as e:
        print(f'Critical error: {e}')


if __name__ == "__main__":
    asyncio.run(main())
