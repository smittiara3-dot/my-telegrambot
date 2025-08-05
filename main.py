import os
import nest_asyncio
from telegram import Update, ReplyKeyboardMarkup
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
    await update.message.reply_text("Залиште номер телефону або email:")
    return CONTACT

async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["contact"] = update.message.text
    await update.message.reply_text("На скільки днів бажаєте орендувати?")
    return DURATION

async def get_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["duration"] = update.message.text

    msg = (
        f"Нова заявка:\nІм'я: {context.user_data['name']}\n"
        f"Локація: {context.user_data['location']}\n"
        f"Книга: {context.user_data['book']}\n"
        f"Контакт: {context.user_data['contact']}\n"
        f"Термін: {context.user_data['duration']} днів"
    )

    await context.bot.send_message(chat_id=ADMIN_ID, text=msg)

    await update.message.reply_text("Дякуємо за заявку! Адміністратор скоро зв'яжеться з вами.")
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
            CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_contact)],
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
