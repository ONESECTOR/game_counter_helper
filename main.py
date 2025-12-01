import asyncio
import json
import logging
from datetime import datetime, timedelta, date, time
from pathlib import Path
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from telegram.error import TelegramError, Forbidden, BadRequest, Forbidden, BadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv

load_dotenv()

from config import (
    BOT_TOKEN,
    CHANNEL_ID,
    START_DATE,
    SEND_TIME,
    MESSAGE_TEMPLATE,
    BOSS_USERNAME,
    BOSS_CHAT_ID,
    VACATION_HASHTAG,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Глобальные переменные
application = None
bot = None
scheduler = AsyncIOScheduler()

# Файл для хранения истории выходных дней
VACATION_HISTORY_FILE = Path("vacation_history.json")

"""
Структура записи в vacation_history.json (по ключу "YYYY-MM-DD"):
{
  "date": "2025-11-14",
  "day_status": "vacation" | "work" | null,
  "question_sent": bool,
  "question_sent_at": "ISO datetime" | null,
  "answered": bool,
  "answered_at": "ISO datetime" | null,
  "answer_source": "boss_button" | "system" | "legacy" | ...,
  "message_sent": bool,
  "message_sent_at": "ISO datetime" | null
}
"""

# Кэш статуса выходного дня в памяти: {date: bool}
vacation_status_cache: dict[date, bool] = {}


def _date_key(d: date) -> str:
    """Преобразует дату в ключ для хранения."""
    return d.isoformat()


def load_vacation_history() -> dict:
    """Загружает историю выходных из файла."""
    if not VACATION_HISTORY_FILE.exists():
        return {}
    try:
        with VACATION_HISTORY_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            return {}
    except Exception as e:
        logger.error(f"Не удалось загрузить историю выходных: {e}")
        return {}


def save_vacation_history(history: dict) -> None:
    """Сохраняет историю выходных в файл."""
    try:
        with VACATION_HISTORY_FILE.open("w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Не удалось сохранить историю выходных: {e}")


def _ensure_day_record(history: dict, target_date: date) -> dict:
    """Гарантирует наличие записи для даты и возвращает её."""
    key = _date_key(target_date)
    info = history.get(key, {})
    if "date" not in info:
        info["date"] = key
    # Значения по умолчанию
    info.setdefault("day_status", None)
    info.setdefault("question_sent", False)
    info.setdefault("question_sent_at", None)
    info.setdefault("answered", False)
    info.setdefault("answered_at", None)
    info.setdefault("answer_source", None)
    info.setdefault("message_sent", False)
    info.setdefault("message_sent_at", None)
    history[key] = info
    return info


def set_day_status(target_date: date, is_vacation: bool, source: str) -> None:
    """Устанавливает статус дня (выходной/рабочий) и сохраняет историю."""
    vacation_status_cache[target_date] = is_vacation

    history = load_vacation_history()
    info = _ensure_day_record(history, target_date)

    info["day_status"] = "vacation" if is_vacation else "work"
    info["answer_source"] = source
    info["answered"] = True
    info["answered_at"] = datetime.now().isoformat(timespec="seconds")

    save_vacation_history(history)
    logger.info(
        f"Статус дня для {target_date} установлен в "
        f"{'выходной' if is_vacation else 'рабочий'} (source={source})"
    )


def get_vacation_status_for_date(target_date: date) -> bool:
    """Возвращает статус выходного для даты с учётом памяти и истории."""
    if target_date in vacation_status_cache:
        return vacation_status_cache[target_date]

    history = load_vacation_history()
    key = _date_key(target_date)
    info = history.get(key)
    if info is None:
        return False

    day_status = info.get("day_status")
    is_vacation = day_status == "vacation"
    vacation_status_cache[target_date] = is_vacation
    return is_vacation


def mark_question_sent_for_date(target_date: date):
    """Помечает, что для указанной даты был отправлен вопрос о выходном."""
    history = load_vacation_history()
    info = _ensure_day_record(history, target_date)
    info["question_sent"] = True
    # Не перезаписываем время если уже есть (например, при повторном вызове)
    if not info.get("question_sent_at"):
        info["question_sent_at"] = datetime.now().isoformat(timespec="seconds")
    save_vacation_history(history)


def mark_message_sent_for_date(target_date: date) -> None:
    """Помечает, что для указанной даты было отправлено сообщение в канал."""
    history = load_vacation_history()
    info = _ensure_day_record(history, target_date)
    info["message_sent"] = True
    info["message_sent_at"] = datetime.now().isoformat(timespec="seconds")
    save_vacation_history(history)


def calculate_day_number():
    today = datetime.now().date()
    start_date = START_DATE.date()
    delta = today - start_date
    return delta.days + 1


def _calculate_day_number_for_date(target_date: date) -> int:
    """Считает номер дня относительно START_DATE для произвольной даты."""
    start_date = START_DATE.date()
    delta = target_date - start_date
    return delta.days + 1


def is_boss_user(update: Update) -> bool:
    """Проверяет, является ли пользователь боссом"""
    if not update.effective_user:
        return False

    # Если задан BOSS_CHAT_ID, проверяем по user_id
    try:
        if BOSS_CHAT_ID:
            boss_id = int(BOSS_CHAT_ID)
            if update.effective_user.id == boss_id:
                return True
    except ValueError:
        # Если BOSS_CHAT_ID не число — игнорируем и падаем обратно на username
        pass

    username = update.effective_user.username
    return bool(username and username.lower() == BOSS_USERNAME.lower())


def _is_conversation_initiated_error(error: Exception) -> bool:
    """Проверяет, является ли ошибка связанной с тем, что бот не может начать диалог"""
    error_msg = str(error).lower()
    return (
        "can't initiate conversation" in error_msg
        or "bot can't initiate" in error_msg
        or "forbidden" in error_msg and "conversation" in error_msg
    )


async def check_boss_availability() -> bool:
    """
    Проверяет, может ли бот отправлять сообщения боссу.
    Возвращает True если доступен, False если нет.
    """
    if not BOSS_CHAT_ID:
        logger.warning("BOSS_CHAT_ID не задан, проверка недоступна")
        return False
    
    if not bot:
        logger.warning("Бот не инициализирован, проверка недоступна")
        return False
    
    try:
        # Пытаемся получить информацию о чате с боссом
        # Если босс не начал диалог, это вызовет Forbidden
        await bot.get_chat(chat_id=BOSS_CHAT_ID)
        logger.info("Босс доступен для отправки сообщений")
        return True
    except Forbidden as e:
        if _is_conversation_initiated_error(e):
            bot_username = "ваш_бот"
            try:
                me = await bot.get_me()
                if me.username:
                    bot_username = f"@{me.username}"
            except:
                pass
            logger.error(
                f"КРИТИЧНО: Бот не может отправить сообщение боссу, "
                f"так как босс не начал диалог с ботом. "
                f"Босс должен найти бота {bot_username} и отправить команду /start"
            )
        else:
            logger.error(f"Доступ к боссу запрещён: {e}")
        return False
    except BadRequest as e:
        logger.error(f"Неверный BOSS_CHAT_ID или пользователь не найден: {e}")
        return False
    except Exception as e:
        logger.warning(f"Не удалось проверить доступность босса: {e}")
        return False


async def check_and_send_message():
    try:
        today = datetime.now().date()
        history = load_vacation_history()
        info = _ensure_day_record(history, today)

        logger.info(f"Проверяем необходимость отправки сообщения за {today}")

        if not info.get("answered"):
            logger.info(
                "На вопрос о выходном за сегодня ещё нет ответа — сообщение не отправляем"
            )
            return

        if info.get("message_sent"):
            logger.info("Сообщение за сегодня уже было отправлено — повтор не нужен")
            return

        current_day = _calculate_day_number_for_date(today)
        message_text = MESSAGE_TEMPLATE.format(current_day)

        # Добавляем хештег выходного для статистики/отображения
        day_status = info.get("day_status")
        is_vacation = day_status == "vacation"
        if is_vacation:
            message_text += VACATION_HASHTAG
            logger.info(f"Добавлен хештег выходного для {today}")

        await bot.send_message(chat_id=CHANNEL_ID, text=message_text)
        logger.info(f"Отправлено сообщение: {message_text}")

        # Помечаем, что сообщение отправлено
        mark_message_sent_for_date(today)

    except TelegramError as e:
        logger.error(f"Ошибка Telegram: {e}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}")

async def ask_about_vacation():
    """Отправляет вопрос о выходном боссу"""
    try:
        if not BOSS_CHAT_ID:
            logger.warning("BOSS_CHAT_ID не задан, пропускаем отправку вопроса")
            return
        today = datetime.now().date()
        date_str = _date_key(today)

        keyboard = [
            [
                InlineKeyboardButton("Да", callback_data=f"vacation_yes:{date_str}"),
                InlineKeyboardButton("Нет", callback_data=f"vacation_no:{date_str}"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await bot.send_message(
            chat_id=BOSS_CHAT_ID,
            text="Босс, сегодня выходной?",
            reply_markup=reply_markup
        )
        logger.info("Отправлен вопрос о выходном боссу")

        # Отмечаем, что вопрос отправлен для сегодняшней даты
        mark_question_sent_for_date(today)

        # Планируем напоминание, если не будет ответа в течение часа
        try:
            reminder_time = datetime.now() + timedelta(hours=1)

            def _wrap_send_reminder():
                return asyncio.create_task(send_vacation_reminder_if_needed())

            scheduler.add_job(
                _wrap_send_reminder,
                trigger=DateTrigger(run_date=reminder_time),
                id=f"vacation_reminder_{_date_key(datetime.now().date())}",
                replace_existing=True,
                name="Напоминание о вопросе о выходном",
            )
            logger.info(
                f"Запланировано напоминание о вопросе о выходном на {reminder_time}"
            )
        except Exception as e:
            logger.error(f"Не удалось запланировать напоминание о выходном: {e}")
    except Forbidden as e:
        if _is_conversation_initiated_error(e):
            bot_username = "ваш_бот"
            try:
                me = await bot.get_me()
                if me.username:
                    bot_username = f"@{me.username}"
            except:
                pass
            logger.error(
                f"КРИТИЧНО: Не удалось отправить вопрос боссу. "
                f"Босс должен найти бота {bot_username} и отправить команду /start, "
                f"чтобы бот мог писать ему сообщения."
            )
        else:
            logger.error(f"Доступ запрещён при отправке вопроса боссу: {e}")
    except TelegramError as e:
        logger.error(f"Ошибка Telegram при отправке вопроса о выходном: {e}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка при отправке вопроса: {e}")


async def send_vacation_reminder_if_needed():
    """Отправляет напоминание, если на вопрос о выходном не ответили."""
    try:
        today = datetime.now().date()
        history = load_vacation_history()
        key = _date_key(today)
        info = history.get(key, {})

        if not info.get("question_sent"):
            logger.info("Вопрос о выходном сегодня не отправлялся — напоминание не нужно")
            return

        if info.get("answered"):
            logger.info("На вопрос о выходном уже ответили — напоминание не нужно")
            return

        if not BOSS_CHAT_ID:
            logger.warning("BOSS_CHAT_ID не задан, пропускаем напоминание")
            return

        date_str = _date_key(today)
        keyboard = [
            [
                InlineKeyboardButton("Да", callback_data=f"vacation_yes:{date_str}"),
                InlineKeyboardButton("Нет", callback_data=f"vacation_no:{date_str}"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await bot.send_message(
            chat_id=BOSS_CHAT_ID,
            text="Напоминание: Босс, сегодня выходной?",
            reply_markup=reply_markup,
        )
        logger.info("Отправлено напоминание о вопросе о выходном боссу")
    except Forbidden as e:
        if _is_conversation_initiated_error(e):
            bot_username = "ваш_бот"
            try:
                me = await bot.get_me()
                if me.username:
                    bot_username = f"@{me.username}"
            except:
                pass
            logger.error(
                f"КРИТИЧНО: Не удалось отправить напоминание боссу. "
                f"Босс должен найти бота {bot_username} и отправить команду /start."
            )
        else:
            logger.error(f"Доступ запрещён при отправке напоминания боссу: {e}")
    except TelegramError as e:
        logger.error(f"Ошибка Telegram при отправке напоминания о выходном: {e}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка при отправке напоминания о выходном: {e}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия на кнопки"""
    query = update.callback_query
    await query.answer()
    
    # Проверяем, что это босс
    if not is_boss_user(update):
        await query.edit_message_text("У вас нет прав для выполнения этого действия.")
        logger.warning(f"Попытка использовать кнопки от неавторизованного пользователя: {update.effective_user.username}")
        return
    
    today = datetime.now().date()

    # Разбираем callback_data, ожидаем формат "vacation_yes:YYYY-MM-DD" / "vacation_no:YYYY-MM-DD"
    data = query.data or ""
    parts = data.split(":", maxsplit=1)
    action = parts[0]
    if len(parts) == 2:
        try:
            target_date = datetime.strptime(parts[1], "%Y-%m-%d").date()
        except ValueError:
            target_date = today
    else:
        target_date = today

    is_today = target_date == today

    if action == "vacation_yes":
        set_day_status(target_date, True, source="boss_button")
        if is_today:
            # Ответ за сегодня: сразу отправляем пост
            await query.edit_message_text(
                "Понял, Босс, отдыхайте и набирайтесь сил, сегодня неплохой для этого день"
            )
        else:
            # Ответ за прошлый день: только статистика
            await query.edit_message_text(
                f"Ответ получен, Босс. Заношу информацию, что день {target_date.isoformat()} был выходным."
            )
        logger.info(f"Установлен выходной для {target_date}")
    elif action == "vacation_no":
        set_day_status(target_date, False, source="boss_button")
        if is_today:
            await query.edit_message_text(
                "Отлично, Босс, не перетруждайтесь, продуктивного Вам дня!"
            )
        else:
            await query.edit_message_text(
                f"Ответ получен, Босс. Заношу информацию, что день {target_date.isoformat()} был рабочим."
            )
        logger.info(f"Выходной не установлен для {target_date}")
    else:
        logger.warning(f"Неизвестное значение callback_data: {data}")
        return

    # Если ответ за сегодня — пытаемся отправить/зафиксировать сообщение за сегодня
    if is_today:
        await check_and_send_message()

async def handle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает команды, проверяя права доступа"""
    if not is_boss_user(update):
        await update.message.reply_text("У вас нет прав для выполнения команд.")
        logger.warning(f"Попытка выполнить команду от неавторизованного пользователя: {update.effective_user.username}")
        return
    
    text = update.message.text or ""
    parts = text.split()
    command = parts[0] if parts else ""

    if command == "/status":
        await handle_status_command(update, context)
    else:
        await update.message.reply_text(
            "Команда получена. Доступные команды:\n"
            "/status - показать статус на сегодня и последние дни\n"
        )


async def handle_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает текущий статус и небольшую историю по выходным."""
    today = datetime.now().date()
    history = load_vacation_history()

    lines = []
    today_key = _date_key(today)
    today_info = _ensure_day_record(history, today)
    day_status = today_info.get("day_status")
    if day_status == "vacation":
        today_status = "выходной"
    elif day_status == "work":
        today_status = "рабочий день"
    else:
        today_status = "статус не определён"
    lines.append(f"Сегодня ({today_key}): {today_status}")

    # Последние 5 дней истории (кроме сегодняшнего)
    keys = sorted(history.keys(), reverse=True)
    other_days = [k for k in keys if k != today_key][:5]
    if other_days:
        lines.append("\nПоследние дни:")
        for k in other_days:
            info = history[k]
            day_status = info.get("day_status")
            if day_status == "vacation":
                status_text = "выходной"
            elif day_status == "work":
                status_text = "рабочий день"
            else:
                status_text = "статус не определён"
            src = info.get("answer_source", "неизвестно")
            lines.append(f"- {k}: {status_text} (source={src})")

    await update.message.reply_text("\n".join(lines))


async def start_scheduler():
    hour, minute = map(int, SEND_TIME.split(':'))
    
    # Задача отправки сообщения в канал (резервный механизм:
    # если к этому времени уже есть ответ, но сообщение ещё не отправлено)
    scheduler.add_job(
        check_and_send_message,
        trigger=CronTrigger(hour=hour, minute=minute),
        id='daily_game_message',
        name='Ежедневное сообщение об игре',
        replace_existing=True
    )
    
    # Задача отправки вопроса о выходном за 3 часа до публикации
    question_hour = (hour - 3) % 24
    scheduler.add_job(
        ask_about_vacation,
        trigger=CronTrigger(hour=question_hour, minute=minute),
        id='daily_vacation_question',
        name='Вопрос о выходном',
        replace_existing=True
    )
    
    scheduler.start()
    logger.info(f"Планировщик запущен. Время отправки: {SEND_TIME}, время вопроса: {question_hour:02d}:{minute:02d}")

async def main():
    global application, bot
    
    if not BOT_TOKEN or not CHANNEL_ID:
        logger.error("Не заданы BOT_TOKEN или CHANNEL_ID")
        return
    
    logger.info("Запуск бота...")
    
    # Создаем Application для обработки обновлений
    application = Application.builder().token(BOT_TOKEN).build()
    bot = application.bot
    
    # Проверяем доступность босса при старте
    if BOSS_CHAT_ID:
        boss_available = await check_boss_availability()
        if not boss_available:
            logger.warning(
                "ВНИМАНИЕ: Бот не может отправлять сообщения боссу. "
                "Босс должен найти бота в Telegram и отправить команду /start, "
                "чтобы бот мог писать ему сообщения."
            )
    
    # Регистрируем обработчики
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(CommandHandler("start", handle_command))
    application.add_handler(CommandHandler("help", handle_command))
    
    # Запускаем обработку обновлений
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    # Запускаем планировщик
    await start_scheduler()
    
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Остановка бота...")
        scheduler.shutdown()
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
