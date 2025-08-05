# main.py

import os
import json
import logging
from dotenv import load_dotenv
from aiohttp import web
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove, KeyboardButton, ReplyKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters
)
import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession

load_dotenv()

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Bot state steps
CHOOSE_LOCATION, CHOOSE_GENRE, SHOW_BOOKS, CHOOSE_BOOK, CHOOSE_RENT_DAYS, GET_NAME, GET_CONTACT, CONFIRM_ORDER = range(8)

# Constants
locations = [f"Кав'ярня {chr(65+i)}" for i in range(20)]
genres = ["Фантастика", "Роман", "Історія", "Детектив"]
rental_days = [10, 14, 21, 30]
books_per_page = 10

books = {
    "Фантастика": [{"title": f"Фантастична книга {i+1}", "description": f"Опис фантастичної книги {i+1}", "price": (i+1)*2} for i in range(15)],
    "Роман": [{"title": "Анна Кареніна", "description": "Класичний роман Льва Толстого", "price": 25}],
    "Історія": [{"title": "Історія України", "description": "Все про історію України", "price": 30}],
    "Детектив": [{"title": "Шерлок Холмс", "description": "Класичний детектив", "price": 22}]
}

# Google Sheets setup
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
json_creds = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
creds_dict = json.loads(json_creds)
credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.Client(auth=credentials)
gc.session = AuthorizedSession(credentials)
worksheet = gc.open("RentalBookBot").sheet1

# BOT HANDLERS

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [[InlineKeyboardButton(loc, callback_data=f"location:{loc}")] for loc in locations[:10]]
    if update.message:
        await update.message.reply_text(
            "👋 *Вас вітає Тиха Поличка!* – зручний сервіс оренди книжок.",
            parse_mode='Markdown'
        )
        await update.message.reply_text("Оберіть локацію:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif update.callback_query:
        await update.callback_query.message.reply_text("Оберіть локацію:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_LOCATION

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    location = query.data.split(":", 1)[1]
    context.user_data["location"] = location
    keyboard = [[InlineKeyboardButton(g, callback_data=f"genre:{g}")] for g in genres]
    keyboard.append([InlineKeyboardButton("📚 Всі книги", callback_data="genre:all")])
    keyboard.append([InlineKeyboardButton("🏠 До локацій", callback_data="back_to_locations")])
    await query.edit_message_text("Оберіть жанр:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SHOW_BOOKS

async def handle_genre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    genre = query.data.split(":", 1)[1]
    context.user_data["genre"] = genre
    book_list = []
    if genre == "all":
        for genre_books in books.values():
            book_list.extend(genre_books)
    else:
        book_list = books.get(genre, [])
    context.user_data["book_list"] = book_list
    keyboard = [[InlineKeyboardButton(b["title"], callback_data=f"book:{b['title']}")] for b in book_list[:books_per_page]]
    keyboard.append([InlineKeyboardButton("🏠 До локацій", callback_data="back_to_locations")])
    keyboard.append([InlineKeyboardButton("🔙 До жанрів", callback_data="back_to_genres")])
    await query.edit_message_text("Оберіть книгу:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_BOOK

async def handle_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    title = query.data.split(":", 1)[1]
    book = next((b for b in context.user_data.get("book_list", []) if b["title"] == title), None)
    if not book:
        await query.edit_message_text("Книгу не знайдено.")
        return SHOW_BOOKS
    context.user_data["selected_book"] = book
    msg = f"📖 *{book['title']}*\n📝 {book['description']}\n💰 Оренда: {book['price']} грн"
    keyboard = [
        [InlineKeyboardButton("✅ Орендувати", callback_data="confirm_rent")],
        [InlineKeyboardButton("🔙 До книг", callback_data="back_to_books")],
        [InlineKeyboardButton("🗂 До жанрів", callback_data="back_to_genres")],
        [InlineKeyboardButton("🏠 До локацій", callback_data="back_to_locations")]
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return CHOOSE_RENT_DAYS

async def choose_rent_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(f"{d} днів", callback_data=f"rent:{d}")] for d in rental_days]
    keyboard.append([InlineKeyboardButton("🔙 До книг", callback_data="back_to_books")])
    await query.edit_message_text("Оберіть кількість днів оренди:", reply_markup=InlineKeyboardMarkup(keyboard))
    return GET_NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    days = int(query.data.split(":", 1)[1])
    context.user_data["days"] = days
    await query.edit_message_text("Введіть, будь ласка, ваше ім'я:")
    return GET_CONTACT

async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text
    button = KeyboardButton("Поділитися номером телефону", request_contact=True)
    await update.message.reply_text("Поділіться своїм номером:", reply_markup=ReplyKeyboardMarkup([[button]], one_time_keyboard=True))
    return CONFIRM_ORDER

async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact.phone_number if update.message.contact else update.message.text
    context.user_data["contact"] = contact

    order = {
        "Локація": context.user_data.get("location"),
        "Жанр": context.user_data.get("genre"),
        "Книга": context.user_data.get("selected_book", {}).get("title"),
        "Ім'я": context.user_data.get("name"),
        "Контакт": contact,
        "Днів": context.user_data.get("days"),
        "Ціна": context.user_data.get("selected_book", {}).get("price", 0)
    }

    worksheet.append_row(list(order.values()))

    msg = (
        f"📚 *Ваше замовлення:*\n"
        f"🏠 Локація: {order['Локація']}\n"
        f"📖 Книга: {order['Книга']}\n"
        f"🗂 Жанр: {order['Жанр']}\n"
        f"📆 Днів: {order['Днів']}\n"
        f"👤 Ім'я: {order['Ім'я']}\n"
        f"📞 Контакт: {order['Контакт']}\n\n"
        f"💰 Сума до оплати: {order['Ціна']} грн"
    )

    keyboard = [[InlineKeyboardButton("💳 Оплатити", url="https://example.com/pay")]]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return ConversationHandler.END

async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Будь ласка, скористайтеся кнопками ⬇️")

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if data.startswith("location:"):
        return await handle_location(update, context)
    elif data.startswith("genre:"):
        return await handle_genre(update, context)
    elif data.startswith("book:"):
        return await handle_book(update, context)
    elif data.startswith("rent:"):
        return await get_name(update, context)
    elif data == "confirm_rent":
        return await choose_rent_days(update, context)
    elif data == "back_to_books":
        return await handle_genre(update, context)
    elif data == "back_to_genres":
        return await handle_location(update, context)
    elif data == "back_to_locations":
        return await start(update, context)
    await update.callback_query.answer("Невідома дія")
    return ConversationHandler.END

# Webhook Handler
async def webhook_handler(request):
    update = await request.json()
    await application.update_queue.put(Update.de_json(update, application.bot))
    return web.Response(text="ok")

# Start Server
async def main():
    global application
    TOKEN = os.getenv("BOT_TOKEN")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    PORT = int(os.getenv("PORT", "8080"))

    application = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_LOCATION: [CallbackQueryHandler(callback_router)],
            CHOOSE_GENRE: [CallbackQueryHandler(callback_router)],
            SHOW_BOOKS: [CallbackQueryHandler(callback_router)],
            CHOOSE_BOOK: [CallbackQueryHandler(callback_router)],
            CHOOSE_RENT_DAYS: [CallbackQueryHandler(callback_router)],
            GET_NAME: [CallbackQueryHandler(callback_router)],
            GET_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_contact)],
            CONFIRM_ORDER: [MessageHandler(filters.CONTACT | filters.TEXT, confirm_order)]
        },
        fallbacks=[MessageHandler(filters.COMMAND, fallback_handler)]
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(callback_router))

    await application.bot.set_webhook(WEBHOOK_URL)
    app = web.Application()
    app.router.add_post("/", webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info(f"Server started on port {PORT}")
    await application.start()
    await application.updater.start_polling()  # required for update queue
    await application.updater.wait()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
