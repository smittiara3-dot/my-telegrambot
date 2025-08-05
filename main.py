import os
import json
import logging
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove, KeyboardButton, ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters, ContextTypes,
)
import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession
import pprint

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation steps
CHOOSE_LOCATION, CHOOSE_GENRE, SHOW_BOOKS, BOOK_DETAILS, CHOOSE_RENT_DAYS, GET_NAME, GET_CONTACT, CONFIRMATION = range(8)

# Config
locations = [f"Кав'ярня {chr(65+i)}" for i in range(20)]
genres = ["Фантастика", "Роман", "Історія", "Детектив"]
rental_days = [10, 14, 21, 30]
books_per_page = 10
locations_per_page = 10

book_data = {
    "Фантастика": [
        {"title": f"Фантастична книга {i}", "desc": f"Це опис фантастичної книги {i}.", "price": 30 + i}
        for i in range(1, 15)
    ],
    "Роман": [
        {"title": "Анна Кареніна", "desc": "Трагічна історія кохання Анни Кареніної.", "price": 40},
        {"title": "Гордість і упередження", "desc": "Класика романтичної літератури.", "price": 35}
    ],
    "Історія": [
        {"title": "Історія України", "desc": "Огляд історії України від давнини до сьогодення.", "price": 50}
    ],
    "Детектив": [
        {"title": "Шерлок Холмс", "desc": "Класичні детективи про Шерлока Холмса.", "price": 45}
    ]
}

SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
creds_dict = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))
credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.Client(auth=credentials)
gc.session = AuthorizedSession(credentials)
sh = gc.open("RentalBookBot")
worksheet = sh.sheet1


def get_paginated_buttons(items, page, prefix, page_size):
    start = page * page_size
    end = start + page_size
    buttons = [[InlineKeyboardButton(name, callback_data=f"{prefix}:{name}")] for name in items[start:end]]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"{prefix}_prev"))
    if end < len(items):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"{prefix}_next"))
    if nav:
        buttons.append(nav)
    return buttons

# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = get_paginated_buttons(locations, 0, "location", locations_per_page)
    text = "👋 *Вас вітає Тиха Поличка!*\nСучасний і зручний спосіб оренди книжок у затишних місцях.\n\nОберіть локацію:"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    context.user_data["location_page"] = 0
    return CHOOSE_LOCATION


async def choose_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    page = context.user_data.get("location_page", 0)

    if data == "location_next":
        context.user_data["location_page"] = page + 1
        keyboard = get_paginated_buttons(locations, page + 1, "location", locations_per_page)
        await query.edit_message_text("Оберіть локацію:", reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSE_LOCATION
    elif data == "location_prev":
        context.user_data["location_page"] = page - 1
        keyboard = get_paginated_buttons(locations, page - 1, "location", locations_per_page)
        await query.edit_message_text("Оберіть локацію:", reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSE_LOCATION

    context.user_data["location"] = data.split(":", 1)[1]

    # Показуємо жанри
    return await show_genres(update, context)


async def show_genres(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Якщо це callback_query — запрос відповіді
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        message_func = query.edit_message_text
    else:
        # Якщо це message (наприклад, з start), - reply_text
        message_func = update.message.reply_text

    keyboard = [[InlineKeyboardButton(genre, callback_data=f"genre:{genre}")] for genre in genres]
    keyboard.append([InlineKeyboardButton("📚 Показати всі книги", callback_data="genre:all")])
    keyboard.append([InlineKeyboardButton("🔙 Назад до локацій", callback_data="back:locations")])

    await message_func("Оберіть жанр:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_GENRE


async def choose_genre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    genre = query.data.split(":", 1)[1]

    if genre == "all":
        all_books = sum(book_data.values(), [])
    else:
        all_books = book_data.get(genre, [])

    if not all_books:
        await query.edit_message_text("Немає книг у цьому жанрі.")
        return ConversationHandler.END

    context.user_data["genre"] = genre
    context.user_data["books"] = all_books
    context.user_data["book_page"] = 0
    return await show_books(update, context)


async def show_books(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    books = context.user_data["books"]
    page = context.user_data.get("book_page", 0)
    start, end = page * books_per_page, (page + 1) * books_per_page
    page_books = books[start:end]

    buttons = [[InlineKeyboardButton(book["title"], callback_data=f"book:{book['title']}")] for book in page_books]
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data="book_prev"))
    if end < len(books):
        nav.append(InlineKeyboardButton("➡️", callback_data="book_next"))
    if nav:
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton("🔙 До жанрів", callback_data="back:genres"),
        InlineKeyboardButton("🔙 До локацій", callback_data="back:locations")
    ])

    await query.edit_message_text("Оберіть книгу:", reply_markup=InlineKeyboardMarkup(buttons))
    return SHOW_BOOKS


async def book_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "book_next":
        context.user_data["book_page"] += 1
    elif query.data == "book_prev":
        context.user_data["book_page"] -= 1
    return await show_books(update, context)


async def book_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    title = query.data.split(":", 1)[1]
    genre = context.user_data["genre"]
    books = book_data.get(genre, []) if genre != "all" else sum(book_data.values(), [])
    book = next((b for b in books if b["title"] == title), None)

    if not book:
        await query.edit_message_text("Книгу не знайдено.")
        return SHOW_BOOKS

    context.user_data["book"] = book
    text = f"*{book['title']}*\n\n{book['desc']}\n\n💸 *Ціна оренди*: {book['price']} грн"
    buttons = [[InlineKeyboardButton(f"{d} днів", callback_data=f"days:{d}")] for d in rental_days]
    buttons.append([
        InlineKeyboardButton("🔙 До книг", callback_data="back:books"),
        InlineKeyboardButton("🔙 До жанрів", callback_data="back:genres"),
        InlineKeyboardButton("🔙 До локацій", callback_data="back:locations")
    ])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return BOOK_DETAILS


async def choose_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["days"] = query.data.split(":", 1)[1]
    # Запит імені через звичайне текстове повідомлення
    await query.edit_message_text("Введіть ваше ім'я:")
    return GET_NAME


async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    button = KeyboardButton("📱 Поділитися номером", request_contact=True)
    reply_markup = ReplyKeyboardMarkup([[button]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Надішліть ваш номер телефону:", reply_markup=reply_markup)
    return GET_CONTACT


async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact.phone_number if update.message.contact else update.message.text.strip()
    context.user_data["contact"] = contact

    # Логування для дебагу
    logger.info("Отримане замовлення: %s", pprint.pformat(context.user_data))

    data = context.user_data
    worksheet.append_row([
        data.get("location", ""), data.get("genre", ""), data.get("book", {}).get("title", ""),
        data.get("days", ""), data.get("name", ""), contact
    ])

    text = (
        f"📚 *Ваше замовлення:*\n"
        f"🏠 Локація: {data['location']}\n"
        f"📖 Книга: {data['book']['title']}\n"
        f"🗂 Жанр: {data['genre']}\n"
        f"📆 Днів: {data['days']}\n"
        f"👤 Ім'я: {data['name']}\n"
        f"📞 Контакт: {data['contact']}\n\n"
        f"Сума до оплати: *{data['book']['price']} грн*"
    )

    button = InlineKeyboardButton("💳 Оплатити", callback_data="pay_now")
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup([[button]]), parse_mode="Markdown")
    return CONFIRMATION


async def confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🎉 Дякуємо за замовлення! Радий бачити вас серед наших читачів ☕")
    return ConversationHandler.END


async def go_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "back:genres":
        return await show_genres(update, context)
    elif data == "back:books":
        return await show_books(update, context)
    elif data == "back:locations":
        return await start(update, context)


def main():
    app = Application.builder().token(os.getenv("BOT_TOKEN")).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_LOCATION: [CallbackQueryHandler(choose_location, pattern="^location.*")],
            CHOOSE_GENRE: [CallbackQueryHandler(choose_genre, pattern="^genre:.*"), CallbackQueryHandler(go_back, pattern="^back:locations$")],
            SHOW_BOOKS: [
                CallbackQueryHandler(book_navigation, pattern="^book_(next|prev)$"),
                CallbackQueryHandler(book_detail, pattern="^book:.*"),
                CallbackQueryHandler(go_back, pattern="^back:(genres|locations)$")
            ],
            BOOK_DETAILS: [CallbackQueryHandler(choose_days, pattern="^days:.*"), CallbackQueryHandler(go_back, pattern="^back:(books|genres|locations)$")],
            CHOOSE_RENT_DAYS: [CallbackQueryHandler(choose_days)],
            GET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            GET_CONTACT: [MessageHandler(filters.CONTACT | filters.TEXT, get_contact)],
            CONFIRMATION: [CallbackQueryHandler(confirm_payment, pattern="^pay_now$")],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("❌ Скасовано."))],
    )

    app.add_handler(conv_handler)
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", 8443)),
        webhook_url=os.getenv("WEBHOOK_URL")
    )

if __name__ == "__main__":
    main()
