import os
import logging
import asyncio
from telegram import ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

logging.basicConfig(level=logging.INFO)

# Conversation steps
LOCATION, BOOK, NAME, CONTACT, DURATION = range(5)

# Прикладні дані
locations = ["Кавʼярня A", "Кавʼярня B"]
books = {
    "Кавʼярня A": ["1984", "Гаррі Поттер", "Майстер і Маргарита"],
    "Кавʼярня B": ["Атлант розправив плечі", "Кобзар", "Великий Гетсбі"]
}

user_data_temp = {}

# Старт
async def start(update, context: ContextTypes.DEFAULT_TYPE):
    reply_keyboard = [locations]
    await update.message.reply_text(
        "Привіт! Обери локацію полиці 📍",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)
    )
    return LOCATION

# Локація
async def get_location(update, context):
    user_data_temp["location"] = update.message.text
    reply_keyboard = [books.get(user_data_temp["location"], [])]
    await update.message.reply_text(
        "Обери книгу 📚",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)
    )
    return BOOK

# Книга
async def get_book(update, context):
    user_data_temp["book"] = update.message.text
    await update.message.reply_text("Введи своє імʼя:")
    return NAME

# Імʼя
async def get_name(update, context):
    user_data_temp["name"] = update.message.text
    await update.message.reply_text("Введи номер телефону або контакт:")
    return CONTACT

# Контакт
async def get_contact(update, context):
    user_data_temp["contact"] = update.message.text
    await update.message.reply_text("На скільки днів хочеш взяти книгу?")
    return DURATION

# Тривалість
async def get_duration(update, context):
    user_data_temp["duration"] = update.message.text
    message = (
        f"Нова оренда книги 📖:\n\n"
        f"🏠 Локація: {user_data_temp['location']}\n"
        f"📚 Книга: {user_data_temp['book']}\n"
        f"👤 Імʼя: {user_data_temp['name']}\n"
        f"📞 Контакт: {user_data_temp['contact']}\n"
        f"🕒 Термін: {user_data_temp['duration']} днів"
    )

    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=message)
    await update.message.reply_text("Дякуємо! Дані передані адміністратору. ✅")

    return ConversationHandler.END

# Скасування
async def cancel(update, context):
    await update.message.reply_text("Дію скасовано.")
    return ConversationHandler.END

# Основний webhook-запуск
async def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_location)],
            BOOK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_book)],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_contact)],
            DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_duration)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)

    # Встановити webhook
    await app.bot.set_webhook(url=WEBHOOK_URL)

    # Запуск
    await app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        webhook_url=WEBHOOK_URL
    )

if __name__ == "__main__":
    asyncio.run(main())
