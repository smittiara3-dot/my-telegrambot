import os
import nest_asyncio
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)

nest_asyncio.apply()

# Стани діалогу
LOCATION, BOOK, NAME, CONTACT, DURATION = range(5)

locations = ["Київ – ЛітКав’ярня", "Львів – BookCup"]
books_catalog = {
    "Київ – ЛітКав’ярня": ["1984 – Дж. Орвелл", "Місто – В. Підмогильний"],
    "Львів – BookCup": ["Тигролови – Іван Багряний", "Фелікс Австрія – Софія Андрухович"],
}

TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_CHAT_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8443))  # За замовчуванням 8443 або 8080, як хочеш

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Вітаємо в боті оренди книжок!\n\nОберіть локацію:",
        reply_markup=ReplyKeyboardMarkup([[loc] for loc in locations], one_time_keyboard=True, resize_keyboard=True),
    )
    return LOCATION

async def get_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["location"] = update.message.text
    books = books_catalog.get(update.message.text, [])
    await update.message.reply_text(
        "Оберіть книгу:",
        reply_markup=ReplyKeyboardMarkup([[b] for b in books], one_time_keyboard=True, resize_keyboard=True),
    )
    return BOOK

async def get_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["book"] = update.message.text
    await update.message.reply_text("Як вас звати?")
    return NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text

    contact_button = ReplyKeyboardMarkup(
        [[KeyboardButton(text="📱 Поділитись номером", request_contact=True)]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )

    await update.message.reply_text(
        "Будь ласка, натисніть кнопку, щоб поділитися своїм номером телефону:",
        reply_markup=contact_button
    )
    return CONTACT

async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.contact:
        context.user_data["contact"] = update.message.contact.phone_number
    else:
        context.user_data["contact"] = update.message.text  # Якщо ввів вручну

    # Кнопки вибору терміну оренди
    duration_buttons = ReplyKeyboardMarkup(
        [
            ["10 днів", "14 днів"],
            ["21 день", "30 днів"]
        ],
        one_time_keyboard=True,
        resize_keyboard=True,
    )

    await update.message.reply_text(
        "Оберіть термін оренди книжки:",
        reply_markup=duration_buttons
    )
    return DURATION

async def get_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["duration"] = update.message.text

    msg = (
        f"Нова заявка:\nІм'я: {context.user_data['name']}\n"
        f"Локація: {context.user_data['location']}\n"
        f"Книга: {context.user_data['book']}\n"
        f"Контакт: {context.user_data['contact']}\n"
        f"Термін: {context.user_data['duration']}"
    )

    await context.bot.send_message(chat_id=ADMIN_ID, text=msg)

    await update.message.reply_text("Дякуємо за заявку!")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Скасовано.")
    return ConversationHandler.END


def main():
    app = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_location)],
            BOOK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_book)],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            CONTACT: [MessageHandler((filters.CONTACT | (filters.TEXT & ~filters.COMMAND)), get_contact)],
            DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_duration)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)

    print(f"Запускаю webhook на порті {PORT}...")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
    )


if __name__ == "__main__":
    main()
