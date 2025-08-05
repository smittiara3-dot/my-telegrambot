
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

CHOOSE_LOCATION, CHOOSE_GENRE, SHOW_BOOKS, CHOOSE_RENT_DAYS, GET_NAME, GET_CONTACT, CONFIRM_ORDER = range(7)

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
    keyboard = [[InlineKeyboardButton(loc, callback_data=f"loc:{i}")] for i, loc in enumerate(locations[:locations_per_page])]
    keyboard.append([InlineKeyboardButton("➡️ Ще локації", callback_data="loc_page:1")])
    await update.message.reply_text("👋 *Вас вітає Тиха Поличка!*
Сучасний і зручний спосіб оренди книжок у затишних для вас місцях.",
                                    parse_mode='Markdown',
                                    reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_LOCATION

async def choose_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    loc_index = int(query.data.split(":")[1])
    context.user_data["location"] = locations[loc_index]
    keyboard = [[InlineKeyboardButton(genre, callback_data=f"genre:{genre}")] for genre in genres]
    keyboard.append([InlineKeyboardButton("📚 Всі книги", callback_data="genre:all")])
    keyboard.append([InlineKeyboardButton("🏠 До локацій", callback_data="back_to_locations")])
    await query.edit_message_text("Оберіть жанр:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_GENRE

async def show_books(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    genre = query.data.split(":")[1]
    context.user_data["genre"] = genre
    all_books = sum(books.values(), []) if genre == "all" else books.get(genre, [])
    context.user_data["book_list"] = all_books
    context.user_data["book_page"] = 0
    return await paginate_books(update, context, 0)

async def paginate_books(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    book_list = context.user_data.get("book_list", [])
    start = page * books_per_page
    end = start + books_per_page
    current_books = book_list[start:end]
    keyboard = [[InlineKeyboardButton(book["title"], callback_data=f"book:{start+i}")] for i, book in enumerate(current_books)]

    nav_buttons = []
    if start > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"book_page:{page-1}"))
    if end < len(book_list):
        nav_buttons.append(InlineKeyboardButton("➡️ Далі", callback_data=f"book_page:{page+1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("🔙 До жанрів", callback_data="back_to_genres")])
    keyboard.append([InlineKeyboardButton("🏠 До локацій", callback_data="back_to_locations")])

    text = "Оберіть книгу:"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return SHOW_BOOKS

async def select_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    index = int(query.data.split(":")[1])
    book = context.user_data["book_list"][index]
    context.user_data["selected_book"] = book

    msg = f"📖 *{book['title']}*

{book['description']}

💸 Ціна: {book['price']} грн"
    keyboard = [
        [InlineKeyboardButton("✅ Орендувати", callback_data="confirm_book")],
        [InlineKeyboardButton("🔙 До книг", callback_data=f"book_page:{context.user_data.get('book_page', 0)}")],
        [InlineKeyboardButton("📚 До жанрів", callback_data="back_to_genres")],
        [InlineKeyboardButton("🏠 До локацій", callback_data="back_to_locations")]
    ]
    await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_RENT_DAYS

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("loc:"):
        return await choose_location(update, context)
    elif data.startswith("loc_page:"):
        page = int(data.split(":")[1])
        start = page * locations_per_page
        end = start + locations_per_page
        keyboard = [[InlineKeyboardButton(loc, callback_data=f"loc:{i}")] for i, loc in enumerate(locations[start:end], start)]
        if end < len(locations):
            keyboard.append([InlineKeyboardButton("➡️ Ще локації", callback_data=f"loc_page:{page+1}")])
        if page > 0:
            keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"loc_page:{page-1}")])
        await query.edit_message_text("Оберіть локацію:", reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSE_LOCATION
    elif data.startswith("genre:"):
        return await show_books(update, context)
    elif data.startswith("book:"):
        return await select_book(update, context)
    elif data.startswith("book_page:"):
        page = int(data.split(":")[1])
        context.user_data["book_page"] = page
        return await paginate_books(update, context, page)
    elif data == "confirm_book":
        return await choose_rent_days(update, context)
    elif data == "back_to_genres":
        return await choose_location(update, context)
    elif data == "back_to_books":
        return await paginate_books(update, context, context.user_data.get("book_page", 0))
    elif data == "back_to_locations":
        return await start(update, context)

def main():
    app = Application.builder().token(os.getenv("BOT_TOKEN")).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_LOCATION: [CallbackQueryHandler(handle_callbacks)],
            CHOOSE_GENRE: [CallbackQueryHandler(handle_callbacks)],
            SHOW_BOOKS: [CallbackQueryHandler(handle_callbacks)],
            CHOOSE_RENT_DAYS: [CallbackQueryHandler(choose_rent_days)],
            GET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            GET_CONTACT: [MessageHandler(filters.CONTACT, confirm_order),
                          MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_order)],
        },
        fallbacks=[],
    )

    app.add_handler(conv_handler)
    app.run_polling()

if __name__ == "__main__":
    main()
