import os
import json
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters, ContextTypes,
)
import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession

load_dotenv()

# Константи станів
CHOOSE_LOCATION, CHOOSE_GENRE, SHOW_BOOKS, SELECT_BOOK, CHOOSE_RENT_DAYS, GET_CONTACT = range(6)

# Дані
locations = ["Кав'ярня A", "Кав'ярня B"]
genres = ["Фантастика", "Роман", "Історія", "Детектив"]
books = {
    "Фантастика": ["Дюна", "1984"],
    "Роман": ["Анна Кареніна", "Гордість і упередження"],
    "Історія": ["Історія України", "Європа ХХ ст."],
    "Детектив": ["Шерлок Холмс", "Вбивство в «Східному експресі»"],
}
rental_days = [10, 14, 21, 30]
books_per_page = 2

# Ініціалізація Google Sheets через JSON зі змінної середовища
json_creds = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
if not json_creds:
    raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON env variable is not set")

creds_dict = json.loads(json_creds)
credentials = Credentials.from_service_account_info(creds_dict)
gc = gspread.Client(auth=credentials)
gc.session = AuthorizedSession(credentials)

sh = gc.open("RentalBookBot")
worksheet = sh.sheet1

# Старт бота
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(loc, callback_data=loc)] for loc in locations]
    await update.message.reply_text("Оберіть локацію:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_LOCATION

# Вибір локації
async def choose_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["location"] = query.data

    keyboard = [[InlineKeyboardButton(genre, callback_data=genre)] for genre in genres]
    keyboard.append([InlineKeyboardButton("Показати всі книги", callback_data="all")])
    await query.edit_message_text(
        "Оберіть жанр або перегляньте всі книжки:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHOOSE_GENRE

# Вибір жанру або всі книги
async def choose_genre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["genre"] = query.data

    all_books = []
    if query.data == "all":
        for book_list in books.values():
            all_books.extend(book_list)
    else:
        all_books = books.get(query.data, [])

    context.user_data["all_books"] = all_books
    context.user_data["page"] = 0
    return await send_book_list(update, context)

# Відправка списку книг із пагінацією
async def send_book_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    books_list = context.user_data["all_books"]
    page = context.user_data.get("page", 0)
    start = page * books_per_page
    end = start + books_per_page
    page_books = books_list[start:end]

    keyboard = [[InlineKeyboardButton(book, callback_data=f"book:{book}")] for book in page_books]

    nav_buttons = []
    if start > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data="prev"))
    if end < len(books_list):
        nav_buttons.append(InlineKeyboardButton("➡️ Далі", callback_data="next"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    await query.edit_message_text("Оберіть книгу:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SHOW_BOOKS

# Обробка пагінації
async def paginate_books(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "next":
        context.user_data["page"] += 1
    elif query.data == "prev":
        context.user_data["page"] -= 1
    return await send_book_list(update, context)

# Вибір книги
async def select_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["book"] = query.data.split(":")[1]

    keyboard = [[InlineKeyboardButton(f"{days} днів", callback_data=str(days))] for days in rental_days]
    await query.edit_message_text("Оберіть кількість днів оренди:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_RENT_DAYS

# Вибір кількості днів оренди
async def choose_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["days"] = query.data

    await query.edit_message_text("Введіть ваш номер телефону або інший контакт:")
    return GET_CONTACT

# Отримання контакту та запис у Google Sheets
async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.text
    context.user_data["contact"] = contact

    worksheet.append_row([
        context.user_data.get("location", ""),
        context.user_data.get("genre", ""),
        context.user_data.get("book", ""),
        context.user_data.get("days", ""),
        contact
    ])

    await update.message.reply_text(
        "Дякуємо! Ваш запит прийнято. Очікуйте підтвердження ☕",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

# Скасування
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Дію скасовано.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# Головна функція
def main():
    app = Application.builder().token(os.getenv("BOT_TOKEN")).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_LOCATION: [CallbackQueryHandler(choose_location)],
            CHOOSE_GENRE: [CallbackQueryHandler(choose_genre)],
            SHOW_BOOKS: [
                CallbackQueryHandler(paginate_books, pattern="^(next|prev)$"),
                CallbackQueryHandler(select_book, pattern="^book:"),
            ],
            SELECT_BOOK: [CallbackQueryHandler(select_book)],
            CHOOSE_RENT_DAYS: [CallbackQueryHandler(choose_days)],
            GET_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_contact)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)

    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 8443)),
        webhook_url=os.getenv("WEBHOOK_URL"),
    )

if __name__ == "__main__":
    main()
