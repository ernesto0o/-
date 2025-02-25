# -*- coding: utf-8 -*-
import re
import sqlite3
import uuid
import logging
from datetime import datetime, timedelta, timezone
from aiogram import Dispatcher, Router, Bot, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    PreCheckoutQuery,
    CallbackQuery,
    LabeledPrice,
    MessageEntity,
)
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
import asyncio

# =========================
# Настройка логирования
# =========================
logging.basicConfig(
    level=logging.INFO,  # Уровень логирования (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',  # Формат сообщений
    handlers=[
        logging.StreamHandler()  # Вывод логов в консоль
    ]
)
logger = logging.getLogger(__name__)

# =========================
# Конфигурация
# =========================
API_TOKEN = ""  # Замените на ваш токен
GROUP_CHAT_ID = '-'    # Замените на ID вашей группы/канала
LOG_CHAT_ID = '-'        # Замените на ID вашего лог-канала
COOLDOWN_SECONDS = 3600            # Ожидание между сообщениями (в секундах)
BAN_DURATION_LINK_HOURS = 48       # Бан за отправку ссылок (в часах)
BAN_DURATION_WORDS_HOURS = 10      # Бан за запрещенные слова (в часах)
PERMANENT_BAN_DATE = "9999-12-31T23:59:59"  # Дата для постоянного бана

# Список администраторов по их user_id
ADMIN_IDS = [123465,]  # Замените на реальные ID админов

# Список запрещенных слов (банвордов)
# Список запрещенных слов (банвордов)
BAN_WORDS = ["ban"]  # Добавьте нужные слова

# =========================
# Настройка бота
# =========================
bot = Bot(token=API_TOKEN, session=AiohttpSession())
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

# =========================
# Определение состояний для FSM
# =========================
class Form(StatesGroup):
    awaiting_message = State()
    awaiting_author_number = State()
    admin_ban = State()
    admin_ban_duration = State()
    admin_ban_reason = State()
    admin_unban = State()
    admin_mailing = State()

# =========================
# Память для отслеживания времени сообщений и статуса
# =========================
last_message_time = {}
user_status = {}

# =========================
# Создание базы данных
# =========================
def setup_db():
    try:
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        # Создание таблицы сообщений
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            message TEXT,
            timestamp TEXT
        )
        """)
        # Создание таблицы банов
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS bans (
            user_id INTEGER PRIMARY KEY,
            ban_until TEXT,
            reason TEXT
        )
        """)
        # Создание таблицы платежей
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            payment_id TEXT PRIMARY KEY,
            user_id INTEGER,
            message_id INTEGER,
            timestamp TEXT,
            status TEXT
        )
        """)
        # Создание таблицы пользователей для рассылки
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT
        )
        """)
        conn.commit()
        logger.info("База данных успешно настроена.")
    except Exception as e:
        logger.error(f"Ошибка при настройке базы данных: {e}")
    finally:
        conn.close()

setup_db()

# =========================
# Функция для преобразования сущностей в HTML
# =========================
def parse_entities(text: str, entities: list[MessageEntity]) -> str:
    """
    Преобразует текст и его сущности в HTML-форматированный текст.
    """
    if not entities:
        return text

    # Сортируем сущности по началу, а затем по длине
    entities = sorted(entities, key=lambda e: (e.offset, -e.length))
    result = ""
    last_index = 0

    for entity in entities:
        # Добавляем текст между предыдущей сущностью и текущей
        result += text[last_index:entity.offset]

        # Извлекаем текст, к которому применяется форматирование
        entity_text = text[entity.offset:entity.offset + entity.length]

        # Применяем соответствующий HTML-тег
        if entity.type == "bold":
            result += f"<b>{entity_text}</b>"
        elif entity.type == "italic":
            result += f"<i>{entity_text}</i>"
        elif entity.type == "underline":
            result += f"<u>{entity_text}</u>"
        elif entity.type == "strikethrough":
            result += f"<s>{entity_text}</s>"
        elif entity.type == "code":
            result += f"<code>{entity_text}</code>"
        elif entity.type == "pre":
            result += f"<pre>{entity_text}</pre>"
        elif entity.type == "text_link":
            href = entity.url
            result += f'<a href="{href}">{entity_text}</a>'
        else:
            # Для остальных типов сущностей просто добавляем текст без форматирования
            result += entity_text

        # Обновляем индекс
        last_index = entity.offset + entity.length

    # Добавляем оставшийся текст после последней сущности
    result += text[last_index:]

    return result

# =========================
# Проверка наличия ссылок в сообщении
# =========================
def contains_link(message: str) -> bool:
    return bool(re.search(r"(https?://|www\.|@|\.ru|\.com|\.org)", message, re.IGNORECASE))

# =========================
# Проверка, забанен ли пользователь
# =========================
def is_banned(user_id: int) -> bool:
    try:
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute("SELECT ban_until, reason FROM bans WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            ban_until_str, reason = row
            if ban_until_str == PERMANENT_BAN_DATE:
                return True
            ban_until = datetime.fromisoformat(ban_until_str).replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) < ban_until:
                return True
            else:
                # Бан истёк, удаляем запись
                cursor.execute("DELETE FROM bans WHERE user_id = ?", (user_id,))
                conn.commit()
                asyncio.create_task(notify_unban(user_id, reason))
        return False
    except Exception as e:
        logger.error(f"Ошибка проверки бана: {e}")
        return False
    finally:
        conn.close()

# =========================
# Уведомление пользователя о бане
# =========================
async def notify_about_ban(user_id: int, username: str, reason: str, ban_until: datetime):
    message = (
        f"🚫 Вы были забанены.\n"
        f"📅 До: {ban_until.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        f"❓ Причина: {reason}"
    )
    try:
        await bot.send_message(user_id, message)
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

    # Отправка уведомления в лог-канал
    try:
        await bot.send_message(
            LOG_CHAT_ID,
            f"🚫 Пользователь: @{username if username else 'пользователь'} (ID: {user_id})\n"
            f"📅 Бан до: {ban_until.strftime('%Y-%m-%d %H:%M:%S') if ban_until else 'Навсегда'}\n"
            f"❓ Причина: {reason}"
        )
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение в лог-канал: {e}")

# =========================
# Уведомление пользователя о разбане
# =========================
async def notify_unban(user_id: int, reason: str):
    message = (
        f"✅ Вы были разбанены.\n"
        f"❓ Причина разбана: {reason}"
    )
    try:
        await bot.send_message(user_id, message)
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

    # Отправка уведомления в лог-канал
    try:
        await bot.send_message(
            LOG_CHAT_ID,
            f"🔓 Пользователь с ID: {user_id} был разбанен.\nПричина разбана: {reason}"
        )
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение в лог-канал: {e}")

# =========================
# Проверка, находится ли пользователь на кулдауне
# =========================
def is_on_cooldown(user_id: int) -> bool:
    if user_id not in last_message_time:
        return False
    last_time = last_message_time[user_id]
    if last_time is None:
        return False
    return (datetime.now() - last_time).total_seconds() < COOLDOWN_SECONDS

# =========================
# Проверка наличия запрещенных слов
# =========================
def contains_ban_word(message: str) -> bool:
    pattern = re.compile(r'\b(' + '|'.join(re.escape(word) for word in BAN_WORDS) + r')\b', re.IGNORECASE)
    return bool(pattern.search(message))

# =========================
# Создание клавиатуры с динамическим добавлением кнопки "🔧 Админка"
# =========================
def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="✉️ Отправить сообщение")],
        [KeyboardButton(text="🔍 Узнать автора сообщения")],
        [KeyboardButton(text="ℹ️ Навигация")]
    ]
    if user_id in ADMIN_IDS:
        buttons.append([KeyboardButton(text="🔧 Админка")])
    keyboard = ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True
    )
    return keyboard

# =========================
# Команда /start с меню
# =========================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()  # Сбрасываем состояние при старте
    keyboard = get_main_keyboard(message.from_user.id)
    welcome_text = (
        "🎄👋 Добро пожаловать!\n"
        "Выберите действие ниже:"
    )
    await message.reply(
        welcome_text,
        reply_markup=keyboard
    )
    # Сохранение пользователя в базе для рассылки
    try:
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
                       (message.from_user.id, message.from_user.username))
        conn.commit()
        logger.info(f"Пользователь {message.from_user.id} ({message.from_user.username}) добавлен в базу данных.")
    except Exception as e:
        logger.error(f"Ошибка при добавлении пользователя в базу: {e}")
    finally:
        conn.close()

# =========================
# Кнопка "✉️ Отправить сообщение"
# =========================
@router.message(F.text == "✉️ Отправить сообщение")
async def ask_question(message: Message, state: FSMContext):
    if is_banned(message.from_user.id):
        await message.reply("❌ Вы забанены!")
        return
    await state.set_state(Form.awaiting_message)
    await message.reply("✉️ Напишите сообщение, которое хотите отправить. Оно будет отправлено в канал. 🎅🎁")

# =========================
# Кнопка "🔍 Узнать автора сообщения"
# =========================
@router.message(F.text == "🔍 Узнать автора сообщения")
async def ask_author(message: Message, state: FSMContext):
    await state.set_state(Form.awaiting_author_number)
    await message.reply("🔧 Эта функция находится в разработке и будет доступна в ближайшее время. Пожалуйста, ожидайте! 🎄")

# =========================
# Кнопка "ℹ️ Навигация"
# =========================
@router.message(F.text == "ℹ️ Навигация")
async def navigation(message: Message):
    navigation_text = (
        "📌 *Навигация по боту:*\n\n"
        "1. *✉️ Отправить сообщение* — Отправьте анонимное сообщение в канал.\n"
        "2. *🔍 Узнать автора сообщения* — 🔧 Эта функция находится в разработке и будет доступна в ближайшее время. Пожалуйста, ожидайте!\n"
        "3. *ℹ️ Навигация* — Получите описание функций бота.\n"
        "4. *🔧 Админка* — Для администраторов бота (доступ ограничен).\n\n"
        "Если у вас есть вопросы, обращайтесь к администратору - [Debugger_24h](https://t.me/Debugger_24h). 🎅🎄"
    )

    await message.reply(navigation_text, parse_mode="Markdown")

# =========================
# Кнопка "🔧 Админка"
# =========================
@router.message(F.text == "🔧 Админка")
async def admin_menu(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("❌ У вас нет доступа к этой кнопке.")
        return

    admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 Забанить пользователя", callback_data="admin_ban")],
        [InlineKeyboardButton(text="✅ Разбанить пользователя", callback_data="admin_unban")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_mailing")]  # Новая кнопка для рассылки
    ])

    await message.reply("🔧 Админка: выберите действие. 🎅🎄", reply_markup=admin_keyboard)

# =========================
# Обработка нажатий на кнопки админки
# =========================
@router.callback_query(F.data.startswith("admin_"))
async def handle_admin_callbacks(callback_query: CallbackQuery, state: FSMContext):
    if callback_query.from_user.id not in ADMIN_IDS:
        await callback_query.answer("❌ У вас нет доступа к этой функции.", show_alert=True)
        return

    action = callback_query.data.split("_")[1]

    if action == "ban":
        await state.set_state(Form.admin_ban)
        await callback_query.message.reply("🛑 Пожалуйста, отправьте ID пользователя или @username для бана.")
    elif action == "unban":
        await state.set_state(Form.admin_unban)
        await callback_query.message.reply("✅ Пожалуйста, отправьте ID пользователя или @username для разбана.")
    elif action == "mailing":
        await state.set_state(Form.admin_mailing)
        await callback_query.message.reply("📢 Пожалуйста, отправьте сообщение для рассылки всем пользователям.\nВы можете прикрепить текст, ссылки и фотографии. 🎄🎅")

    await callback_query.answer()  # Закрываем уведомление

# =========================
# Обработка сообщений для админки и других состояний
# =========================
@router.message(F.chat.type == "private")
async def handle_private_message(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or "Без имени"
    text = message.text or message.caption or ""

    current_state = await state.get_state()

    if current_state == Form.awaiting_message:
        # Обработка отправки сообщения
        if is_banned(user_id):
            await message.reply("❌ Вы забанены!")
            await state.clear()
            return

        if contains_link(text):
            ban_duration_hours = 48  # Бан за ссылки
            ban_until = datetime.now(timezone.utc) + timedelta(hours=ban_duration_hours)
            reason = "Отправка ссылок."
            try:
                conn = sqlite3.connect("bot_database.db")
                cursor = conn.cursor()
                cursor.execute("INSERT OR REPLACE INTO bans (user_id, ban_until, reason) VALUES (?, ?, ?)",
                               (user_id, ban_until.isoformat(), reason))
                conn.commit()
            except Exception as e:
                logger.error(f"Ошибка при бане пользователя: {e}")
                await message.reply("❌ Произошла ошибка при бане.")
                await state.clear()
                return
            finally:
                conn.close()
            await notify_about_ban(user_id, username, reason, ban_until)
            await message.reply("❌ Вы забанены на 48 часов за отправку ссылок.")
            await state.clear()
            return

        if contains_ban_word(text):
            ban_duration_hours = 10  # Бан за запрещенные слова
            ban_until = datetime.now(timezone.utc) + timedelta(hours=ban_duration_hours)
            reason = "Использование запрещенных слов."
            try:
                conn = sqlite3.connect("bot_database.db")
                cursor = conn.cursor()
                cursor.execute("INSERT OR REPLACE INTO bans (user_id, ban_until, reason) VALUES (?, ?, ?)",
                               (user_id, ban_until.isoformat(), reason))
                conn.commit()
            except Exception as e:
                logger.error(f"Ошибка при бане пользователя: {e}")
                await message.reply("❌ Произошла ошибка при бане.")
                await state.clear()
                return
            finally:
                conn.close()
            await notify_about_ban(user_id, username, reason, ban_until)
            await message.reply("❌ Вы забанены на 10 часов за использование запрещенных слов.")
            await state.clear()
            return

        if (message.photo or message.video or message.animation) and not text.strip():
            await message.reply("❌ Добавьте текст к вашему медиафайлу (фото, видео или GIF). 🎄")
            return

        if message.video and message.video.duration > 5:
            await message.reply("❌ Видео не может быть длиннее 5 секунд. 🎅")
            return

        if message.document:
            await message.reply("❌ Отправка файлов запрещена. ")
            return

        if is_on_cooldown(user_id):
            wait_time = COOLDOWN_SECONDS - int((datetime.now() - last_message_time[user_id]).total_seconds())
            await message.reply(f"⏳ Пожалуйста, подождите {wait_time} секунд.")
            return

        last_message_time[user_id] = datetime.now()
        try:
            conn = sqlite3.connect("bot_database.db")
            cursor = conn.cursor()
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("INSERT INTO messages (user_id, username, message, timestamp) VALUES (?, ?, ?, ?)",
                           (user_id, username, text.strip(), timestamp))
            message_id = cursor.lastrowid  # Получение ID сообщения
            conn.commit()
            logger.info(f"Сообщение #{message_id} от пользователя {user_id} сохранено.")
        except Exception as e:
            logger.error(f"Ошибка при сохранении сообщения: {e}")
            await message.reply("❌ Произошла ошибка при сохранении вашего сообщения.")
            await state.clear()
            return
        finally:
            conn.close()

        await bot.send_message(
            LOG_CHAT_ID,
            f"👤 Пользователь: @{username} - {user_id}\n⏰ Время: {timestamp}\n📝 Сообщение: {text.strip()}"
        )

        # Получение сущностей из сообщения
        entities = message.entities or message.caption_entities or []

        # Преобразование текста с сущностями в HTML
        formatted_text = parse_entities(text, entities)

        caption = (
            f"📩 Новое сообщение! 🎄\n\n"
            f"Сообщение: {formatted_text}\n\n"
            f"Чтобы задать вопрос, пишите @n"
            f"№{message_id}."
        )

        try:
            if message.photo:
                await bot.send_photo(
                    GROUP_CHAT_ID,
                    message.photo[-1].file_id,
                    caption=caption,
                    parse_mode="HTML"  # Используем HTML для форматирования
                )
            elif message.video:
                await bot.send_video(
                    GROUP_CHAT_ID,
                    message.video.file_id,
                    caption=caption,
                    parse_mode="HTML"
                )
            elif message.animation:
                await bot.send_animation(
                    GROUP_CHAT_ID,
                    message.animation.file_id,
                    caption=caption,
                    parse_mode="HTML"
                )
            else:
                await bot.send_message(
                    GROUP_CHAT_ID,
                    caption,
                    parse_mode="HTML"
                )
            logger.info(f"Сообщение #{message_id} отправлено в группу.")
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения в группу: {e}")
            await message.reply("❌ Произошла ошибка при отправке вашего сообщения в группу.")
            await state.clear()
            return

        await message.reply("✅ Ваше сообщение отправлено! 🎅🎄")
        await state.clear()

    elif current_state == Form.awaiting_author_number:
        # Обработка запроса на автора сообщения
        try:
            message_id = int(text.strip())
        except ValueError:
            await message.reply("🔧 Введите корректный номер сообщения.")
            return

        # Проверка наличия сообщения в базе
        try:
            conn = sqlite3.connect("bot_database.db")
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, username, message, timestamp FROM messages WHERE id = ?", (message_id,))
            row = cursor.fetchone()
        except Exception as e:
            logger.error(f"Ошибка при запросе сообщения: {e}")
            row = None
        finally:
            conn.close()

        if not row:
            await message.reply(f"❌ Сообщение с номером #{message_id} не найдено.")
            await state.clear()
            return

        # Проверка, оплатил ли пользователь доступ к этому сообщению
        try:
            conn = sqlite3.connect("bot_database.db")
            cursor = conn.cursor()
            cursor.execute("""
                SELECT status FROM payments
                WHERE user_id = ? AND message_id = ? AND status = 'completed'
            """, (user_id, message_id))
            payment_row = cursor.fetchone()
        except Exception as e:
            logger.error(f"Ошибка при проверке платежа: {e}")
            payment_row = None
        finally:
            conn.close()

        if payment_row:
            # Пользователь оплатил, предоставляем информацию
            author_user_id, author_username, msg, timestamp_msg = row
            response = (
                f"✅ Информация о сообщении #{message_id}:\n\n"
                f"**Отправитель:** @{author_username} (ID: {author_user_id})\n"
                f"**Текст:** {msg}\n"
                f"**Дата:** {timestamp_msg}"
            )
            await message.reply(response, parse_mode="Markdown")
        else:
            # Инициируем оплату за доступ
            prices = [LabeledPrice(label="Доступ к информации об авторе", amount=100000)]  # 1.00 руб (замените на нужную сумму)
            unique_id = uuid.uuid4()  # Генерируем уникальный идентификатор
            payload = f"message_{message_id}_{user_id}_{unique_id}"  # Добавляем UUID к payload

            await bot.send_invoice(
                chat_id=message.chat.id,
                title=f"Доступ к сообщению #{message_id}",
                description="🔧 Оплатите доступ к информации об авторе сообщения.",
                provider_token='YOUR_PROVIDER_TOKEN',  # Замените на ваш provider_token
                currency="RUB",  # Замените на нужную валюту, например, "USD"
                prices=prices,
                payload=payload,
                start_parameter=f"buy_message_{message_id}",
                reply_markup=None  # Кнопка оплаты встроена в метод send_invoice
            )

            await message.reply("🔧 Оплатите доступ к информации об авторе сообщения. 🎄")

        await state.clear()

    elif current_state == Form.admin_ban:
        # Админ вводит пользователя для бана
        target = text.strip()
        target_user_id, target_username = await resolve_user(target)
        if not target_user_id:
            await message.reply(f"❌ Не удалось найти пользователя с идентификатором или @username: {target}")
            await state.clear()
            return

        # Сохраняем target_user_id и target_username в контексте для дальнейшего использования
        await state.update_data(target_user_id=target_user_id, target_username=target_username)
        await state.set_state(Form.admin_ban_duration)
        await message.reply("🗓️ Введите количество дней для бана:")

    elif current_state == Form.admin_ban_duration:
        # Админ вводит длительность бана
        try:
            days = int(text.strip())
            if days <= 0:
                raise ValueError
        except ValueError:
            await message.reply("❌ Пожалуйста, введите корректное количество дней (целое число больше 0).")
            return

        user_data = await state.get_data()
        target_user_id = user_data.get("target_user_id")
        target_username = user_data.get("target_username")

        await state.update_data(ban_duration_days=days)
        await state.set_state(Form.admin_ban_reason)
        await message.reply("📋 Введите причину бана:")

    elif current_state == Form.admin_ban_reason:
        # Админ вводит причину бана
        reason = text.strip()
        user_data = await state.get_data()
        target_user_id = user_data.get("target_user_id")
        target_username = user_data.get("target_username")
        ban_duration_days = user_data.get("ban_duration_days")

        ban_until = datetime.now(timezone.utc) + timedelta(days=ban_duration_days)
        try:
            conn = sqlite3.connect("bot_database.db")
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO bans (user_id, ban_until, reason) VALUES (?, ?, ?)",
                           (target_user_id, ban_until.isoformat(), reason))
            conn.commit()
            logger.info(f"Пользователь {target_user_id} ({target_username}) забанен до {ban_until.isoformat()} по причине: {reason}")
        except Exception as e:
            logger.error(f"Ошибка при бане пользователя: {e}")
            await message.reply("❌ Произошла ошибка при бане пользователя.")
            await state.clear()
            return
        finally:
            conn.close()

        await notify_about_ban(target_user_id, target_username, reason, ban_until)
        await message.reply(
            f"✅ Пользователь @{target_username} (ID: {target_user_id}) был забанен на {ban_duration_days} дней.\nПричина: {reason}"
        )

        # Запуск задачи для автоматического разбана
        asyncio.create_task(schedule_unban(target_user_id, ban_until, reason))

        await state.clear()

    elif current_state == Form.admin_unban:
        # Обработка разбана через админку
        target = text.strip()
        target_user_id, target_username = await resolve_user(target)
        if not target_user_id:
            await message.reply(f"❌ Не удалось найти пользователя с идентификатором или @username: {target}")
            await state.clear()
            return

        reason = "Админская команда: разбан."

        try:
            conn = sqlite3.connect("bot_database.db")
            cursor = conn.cursor()
            cursor.execute("DELETE FROM bans WHERE user_id = ?", (target_user_id,))
            conn.commit()
            logger.info(f"Пользователь {target_user_id} ({target_username}) разбанен по причине: {reason}")
        except Exception as e:
            logger.error(f"Ошибка при разбане пользователя: {e}")
            await message.reply("❌ Произошла ошибка при разбане пользователя.")
            await state.clear()
            return
        finally:
            conn.close()

        await notify_unban(target_user_id, reason)
        await message.reply(f"✅ Пользователь @{target_username} (ID: {target_user_id}) был разбанен.")

        await state.clear()

    elif current_state == Form.admin_mailing:
        # Обработка рассылки
        try:
            conn = sqlite3.connect("bot_database.db")
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users")
            users = cursor.fetchall()
            logger.info(f"Получено {len(users)} пользователей для рассылки.")
        except Exception as e:
            logger.error(f"Ошибка при получении списка пользователей для рассылки: {e}")
            users = []
        finally:
            conn.close()

        if not users:
            await message.reply("❌ Нет пользователей для рассылки.")
            await state.clear()
            return

        send_text = text  # Используем 'text', которое включает 'message.text or message.caption or ""'
        media = None
        media_type = None

        if message.photo:
            media = message.photo[-1].file_id
            media_type = 'photo'
        elif message.video:
            media = message.video.file_id
            media_type = 'video'
        elif message.animation:
            media = message.animation.file_id
            media_type = 'animation'

        # Получение сущностей из сообщения для рассылки
        entities = message.entities or message.caption_entities or []

        # Преобразование текста с сущностями в HTML
        formatted_text = parse_entities(text, entities)

        send_text = formatted_text  # Используем отформатированный текст

        sent_count = 0
        failed_count = 0

        for user in users:
            try:
                if media and media_type == 'photo':
                    await bot.send_photo(
                        user[0],
                        media,
                        caption=send_text,
                        parse_mode="HTML"  # Используем HTML для форматирования
                    )
                elif media and media_type == 'video':
                    await bot.send_video(
                        user[0],
                        media,
                        caption=send_text,
                        parse_mode="HTML"
                    )
                elif media and media_type == 'animation':
                    await bot.send_animation(
                        user[0],
                        media,
                        caption=send_text,
                        parse_mode="HTML"
                    )
                else:
                    await bot.send_message(
                        user[0],
                        send_text,
                        parse_mode="HTML"
                    )
                sent_count += 1
                await asyncio.sleep(0.05)  # Пауза между отправками, чтобы избежать ограничений Telegram
            except Exception as e:
                logger.error(f"Не удалось отправить сообщение пользователю {user[0]}: {e}")
                failed_count += 1

        await message.reply(f"📢 Рассылка завершена.\nУспешно отправлено: {sent_count}\nНе удалось отправить: {failed_count}")
        await state.clear()

    else:
        # Игнорируем все остальные сообщения, не относящиеся к текущим состояниям
        pass  # Бот не отвечает на неучтенные сообщения

# =========================
# Функция для получения user_id и username по @username или user_id
# =========================
async def resolve_user(target: str):
    if target.startswith("@"):
        username_target = target[1:]
        try:
            user = await bot.get_chat(username_target)
            # Проверяем, что это пользователь, а не канал или группа
            if user.type == "private":
                return user.id, user.username or "Без имени"
            else:
                logger.warning(f"Нельзя банить {user.type}: @{username_target}")
                return None, None
        except Exception as e:
            logger.error(f"Не удалось найти пользователя {username_target}: {e}")
            return None, None
    else:
        try:
            target_user_id = int(target)
            try:
                user = await bot.get_chat(target_user_id)
                # Проверяем, что это пользователь, а не канал или группа
                if user.type == "private":
                    return user.id, user.username or "Без имени"
                else:
                    logger.warning(f"Нельзя банить {user.type}: {target_user_id}")
                    return None, None
            except Exception as e:
                logger.error(f"Не удалось найти пользователя с ID {target_user_id}: {e}")
                return None, None
        except ValueError:
            logger.error(f"Неверный формат пользователя: {target}")
            return None, None

# =========================
# Функция для автоматического разбана
# =========================
async def schedule_unban(user_id: int, ban_until: datetime, reason: str):
    now = datetime.now(timezone.utc)
    delay = (ban_until - now).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bans WHERE user_id = ?", (user_id,))
        conn.commit()
        logger.info(f"Пользователь {user_id} автоматически разбанен.")
    except Exception as e:
        logger.error(f"Ошибка при автоматическом разбане пользователя {user_id}: {e}")
    finally:
        conn.close()
    await notify_unban(user_id, reason)

# =========================
# Обработка успешной оплаты
# =========================
@router.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@router.message(F.successful_payment)
async def success_payment_handler(message: Message, state: FSMContext):
    payment = message.successful_payment
    payload = payment.invoice_payload
    user_id = message.from_user.id
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    try:
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()

        if payload.startswith("message_"):
            # Оплата за доступ к сообщению
            parts = payload.split("_")
            if len(parts) < 4:
                await message.reply("❌ Неверный формат оплаты.")
                return
            message_id = int(parts[1])
            payer_id = int(parts[2])
            # unique_id = parts[3]  # Не используем, так как уже ассоциировали оплату с message_id и user_id

            cursor.execute("""
                INSERT INTO payments (payment_id, user_id, message_id, timestamp, status)
                VALUES (?, ?, ?, ?, ?)
            """, (
                payment.provider_payment_charge_id,
                payer_id,
                message_id,
                timestamp,
                "completed"
            ))
            conn.commit()
            logger.info(f"Платеж {payment.provider_payment_charge_id} от пользователя {payer_id} за сообщение {message_id} обработан.")
            await message.reply("🥳 Спасибо за оплату! Теперь вы можете узнать информацию об авторе сообщения.")
        else:
            # Неизвестный payload
            await message.reply("❌ Неизвестная оплата.")
    except Exception as e:
        logger.error(f"Ошибка при обработке платежа: {e}")
        await message.reply("❌ Произошла ошибка при обработке вашего платежа.")
    finally:
        conn.close()

# =========================
# Команда для тестирования форматирования
# =========================
@router.message(Command(commands=["test_format"]))
async def test_format(message: Message):
    test_message = (
        "<b>Тестовое сообщение</b>\n"
        "<i>Это курсив</i>\n"
        "<a href='https://example.com'>Ссылка на сайт</a>\n"
        "<code>Моноширинный текст</code>"
    )
    try:
        await bot.send_message(
            message.chat.id,
            test_message,
            parse_mode="HTML"
        )
        await message.reply("✅ Тестовое сообщение отправлено.")
    except Exception as e:
        logger.error(f"Ошибка при отправке тестового сообщения: {e}")
        await message.reply("❌ Не удалось отправить тестовое сообщение.")

# =========================
# Асинхронный запуск бота
# =========================
if __name__ == "__main__":
    async def main():
        try:
            dp.include_router(router)
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("Бот успешно запущен.")
            await dp.start_polling(bot)
        finally:
            await bot.session.close()
            logger.info("Сессия бота закрыта.")

    asyncio.run(main())
