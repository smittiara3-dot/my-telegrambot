import os
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ConversationHandler, ContextTypes

# Стани діалогу
LOCATION, BOOK, NAME, CONTACT, DURATION = range(5)

# Дані
locations = ["Київ – ЛітКав’ярня", "Львів – BookCup"]
books_catalog = {
    "Київ – ЛітКав’ярня": ["1984 – Дж. Орвелл", "Місто – В. Підмогильний"],
    "Львів – BookCup": ["Тигролови – Іван Багряний", "Фелікс Австрія – Софія Андрухович"]
}

TOKEN = os.getenv("8409973335:AAHaYO-_K8_gPdcYPtO7ycWerEUFq4bgPpk")
ADMIN_ID = int(os.getenv("1332202691"))  # Твій Telegram ID

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Вітаємо в боті *оренди книжок* у кав'ярнях!\n\n"
        "📚 Вибирай книжку – читай на місці або бери з собою!\n\n"
        "Давай оберемо локацію 📍",
        reply_markup=ReplyKeyboardMarkup([[l] for l in locations], one_time_keyboard=True, resize_keyboard=True)
    )
    return LOCATION

async def get_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["location"] = update.message.text
    books = books_catalog.get(update.message.text, [])
    await update.message.reply_text(
        "Ось доступні книжки на цій локації:\n📚",
        reply_markup=ReplyKeyboardMarkup([[b] for b in books], one_time_keyboard=True, resize_keyboard=True)
    )
    return BOOK

async def get_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["book"] = update.message.text
    await update.message.reply_text("Як вас звати?")
    return NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text
    await update.message.reply_text("Залиште контактний номер телефону або email:")
    return CONTACT

async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["contact"] = update.message.text
    await update.message.reply_text("На скільки днів бажаєте орендувати книжку?")
    return DURATION

async def get_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["duration"] = update.message.text

    msg = (
        "📦 Нова заявка на оренду:\n"
        f"👤 Ім’я: {context.user_data['name']}\n"
        f"📍 Локація: {context.user_data['location']}\n"
        f"📚 Книга: {context.user_data['book']}\n"
        f"📞 Контакт: {context.user_data['contact']}\n"
        f"🕓 Термін: {context.user_data['duration']} днів"
    )

    await context.bot.send_message(chat_id=ADMIN_ID, text=msg)

    await update.message.reply_text(
        "✅ Дякуємо за заявку!\n"
        "Наш адміністратор скоро з вами зв’яжеться.\n\n"
        "📍 Забрати книжку можна на обраній локації. Приємного читання!"
    )

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Скасовано.")
    return ConversationHandler.END

# --- MAIN ---

app = ApplicationBuilder().token(TOKEN).build()

conv = ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={
        LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_location)],
        BOOK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_book)],
        NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
        CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_contact)],
        DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_duration)],
    },
    fallbacks=[CommandHandler("cancel", cancel)]
)

app.add_handler(conv)
print("Бот працює...")

app.run_polling()
