import os
import json
import logging
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove, KeyboardButton, ReplyKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters, ContextTypes,
)
import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CHOOSE_LOCATION, CHOOSE_GENRE, SHOW_BOOKS, CHOOSE_BOOK, CHOOSE_RENT_DAYS, GET_NAME, GET_CONTACT, CONFIRM_ORDER = range(8)

locations = [f"Кав'ярня {chr(65+i)}" for i in range(20)]
genres = ["Фантастика", "Роман", "Історія", "Детектив"]
rental_days = [10, 14, 21, 30]
books_per_page = 10
locations_per_page = 10

books = {
    "Фантастика": [
        {"title": f"Фантастична книга {i+1}", "description": f"Опис фантастичної книги {i+1}", "price": (i+1)*2} for i in range(15)
    ],
    "Роман": [
        {"title": "Анна Кареніна", "description": "Класичний роман Льва Толстого", "price": 25},
        {"title": "Гордість і упередження", "description": "Роман Джейн Остін про кохання і гордість", "price": 20},
    ],
    "Історія": [
        {"title": "Історія України", "description": "Все про історію України", "price": 30},
        {"title": "Європа ХХ ст.", "description": "Історичні події Європи у 20 столітті", "price": 28},
    ],
    "Детектив": [
        {"title": "Шерлок Холмс", "description": "Класичний детектив про Шерлока", "price": 22},
        {"title": "Вбивство в «Східному експресі»", "description": "Детектив від Агати Крісті", "price": 24},
    ],
}

SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

json_creds = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
if not json_creds:
    raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON env variable is not set")

creds_dict = json.loads(json_creds)
credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.Client(auth=credentials)
gc.session = AuthorizedSession(credentials)
sh = gc.open("RentalBookBot")
worksheet = sh.sheet1

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton(loc, callback_data=f"location:{loc}")] for loc in locations[:locations_per_page]
    ]
    await update.message.reply_text(
        "👋 *Вас вітає Тиха Поличка!* – сучасний і зручний спосіб оренди книжок у затишних для вас місцях.",
        parse_mode='Markdown'
    )
    await update.message.reply_text("Оберіть локацію:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_LOCATION

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    location = query.data.split(":")[1]
    context.user_data["location"] = location
    keyboard = [[InlineKeyboardButton(g, callback_data=f"genre:{g}")] for g in genres]
    keyboard.append([InlineKeyboardButton("📚 Всі книги", callback_data="genre:all")])
    keyboard.append([InlineKeyboardButton("🏠 До локацій", callback_data="back_to_locations")])
    await query.edit_message_text("Оберіть жанр:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SHOW_BOOKS

async def handle_genre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    genre = query.data.split(":")[1]
    context.user_data["genre"] = genre
    all_books = []
    if genre == "all":
        for g in books.values():
            all_books.extend(g)
    else:
        all_books = books.get(genre, [])

    context.user_data["book_list"] = all_books

    keyboard = []
    for b in all_books[:books_per_page]:
        keyboard.append([InlineKeyboardButton(b["title"], callback_data=f"book:{b['title']}")])

    keyboard.append([InlineKeyboardButton("🏠 До локацій", callback_data="back_to_locations")])
    keyboard.append([InlineKeyboardButton("🔙 До жанрів", callback_data="back_to_genres")])
    await query.edit_message_text("Оберіть книгу:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_BOOK

async def handle_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    title = query.data.split(":")[1]
    book_list = context.user_data.get("book_list", [])
    book = next((b for b in book_list if b["title"] == title), None)
    if not book:
        await query.edit_message_text("Книгу не знайдено.")
        return SHOW_BOOKS
    context.user_data["selected_book"] = book

    msg = f"📖 *{book['title']}*\n"
    msg += f"📝 {book['description']}\n"
    msg += f"💰 Оренда: {book['price']} грн"
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
    keyboard.append([InlineKeyboardButton("🏠 До локацій", callback_data="back_to_locations")])
    await query.edit_message_text("Оберіть кількість днів оренди:", reply_markup=InlineKeyboardMarkup(keyboard))
    return GET_NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rent_days = int(query.data.split(":")[1])
    context.user_data["days"] = rent_days
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

    loc = context.user_data.get("location", "[не вибрано]")
    genre = context.user_data.get("genre", "[не вибрано]")
    book = context.user_data.get("selected_book", {})
    name = context.user_data.get("name", "[не вказано]")
    days = context.user_data.get("days", 0)
    price = book.get("price", 0)
    contact = context.user_data.get("contact", "[не вказано]")

    worksheet.append_row([loc, genre, book.get("title", "-"), name, contact, days, price])

    msg = f"📚 *Ваше замовлення:*\n"
    msg += f"🏠 Локація: {loc}\n"
    msg += f"📖 Книга: {book.get('title', '-')}\n"
    msg += f"🗂 Жанр: {genre}\n"
    msg += f"📆 Днів: {days}\n"
    msg += f"👤 Ім'я: {name}\n"
    msg += f"📞 Контакт: {contact}\n\n"
    msg += f"Сума до оплати: {price} грн"

    keyboard = [[InlineKeyboardButton("💳 Оплатити", url="https://example.com/pay")]]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    await update.message.reply_text("Дякуємо, що обрали наш сервіс! 📖")
    return ConversationHandler.END

async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Скористайтесь кнопками для навігації.")

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

def main():
    app = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()

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

    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(callback_router))

    app.run_polling()

if __name__ == '__main__':
    main()
