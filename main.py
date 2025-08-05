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

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CHOOSE_LOCATION, CHOOSE_GENRE, SHOW_BOOKS, CHOOSE_RENT_DAYS, GET_NAME, GET_CONTACT, CONFIRM_BOOK = range(7)

locations = [f"Кав'ярня {i}" for i in range(1, 21)]
genres = ["Фантастика", "Роман", "Історія", "Детектив"]
books = {
    "Фантастика": ["Дюна", "1984"],
    "Роман": ["Анна Кареніна", "Гордість і упередження"],
    "Історія": ["Історія України", "Європа ХХ ст."],
    "Детектив": ["Шерлок Холмс", "Вбивство в «Східному експресі»"],
}
book_descriptions = {
    "Дюна": ("Епічна сага про пустельну планету Аракіс.", "50 грн"),
    "1984": ("Антиутопія про тоталітарне майбутнє.", "45 грн"),
    "Анна Кареніна": ("Класика про кохання і зраду.", "40 грн"),
    "Гордість і упередження": ("Історія Елізабет Беннет і містера Дарсі.", "42 грн"),
    "Історія України": ("Огляд історії України з давніх часів.", "55 грн"),
    "Європа ХХ ст.": ("Події та зміни в Європі ХХ століття.", "50 грн"),
    "Шерлок Холмс": ("Розслідування детектива Холмса.", "38 грн"),
    "Вбивство в «Східному експресі»": ("Детективна історія Агати Крісті.", "44 грн"),
}
rental_days = [10, 14, 21, 30]
items_per_page = 10

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
    logger.info("/start від %s", update.effective_user.first_name)
    keyboard = [[InlineKeyboardButton(loc, callback_data=loc)] for loc in locations[:items_per_page]]
    nav_buttons = []
    if len(locations) > items_per_page:
        nav_buttons.append(InlineKeyboardButton("➡️ Далі", callback_data="loc_next_1"))
        keyboard.append(nav_buttons)

    await update.message.reply_text(
        "👋 *Вас вітає Тиха Поличка!*\n"
        "Сучасний і зручний спосіб оренди книжок у затишних для вас місцях.",
        parse_mode="Markdown"
    )
    await update.message.reply_text("Оберіть локацію:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_LOCATION

async def paginate_locations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, direction, page = query.data.split("_")
    page = int(page)
    start = page * items_per_page
    end = start + items_per_page
    keyboard = [[InlineKeyboardButton(loc, callback_data=loc)] for loc in locations[start:end]]
    nav_buttons = []
    if start > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"loc_prev_{page - 1}"))
    if end < len(locations):
        nav_buttons.append(InlineKeyboardButton("➡️ Далі", callback_data=f"loc_next_{page + 1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)
    await query.edit_message_text("Оберіть локацію:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_LOCATION

async def choose_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["location"] = query.data
    keyboard = [[InlineKeyboardButton(genre, callback_data=genre)] for genre in genres]
    keyboard.append([InlineKeyboardButton("Показати всі книги", callback_data="all")])
    await query.edit_message_text("Оберіть жанр або перегляньте всі книжки:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_GENRE

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

    if not all_books:
        await query.edit_message_text("На жаль, наразі немає доступних книжок у цьому жанрі.")
        return ConversationHandler.END

    context.user_data["all_books"] = all_books
    context.user_data["page"] = 0
    return await send_book_list(update, context)

async def send_book_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    books_list = context.user_data["all_books"]
    page = context.user_data.get("page", 0)
    start = page * items_per_page
    end = start + items_per_page
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

async def paginate_books(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "next":
        context.user_data["page"] += 1
    elif query.data == "prev":
        context.user_data["page"] -= 1
    return await send_book_list(update, context)

async def select_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    book_name = query.data.split(":")[1]
    context.user_data["book"] = book_name
    description, price = book_descriptions.get(book_name, ("Немає опису", "Ціна не вказана"))
    keyboard = [[InlineKeyboardButton("Підтвердити оренду", callback_data="confirm")],
                [InlineKeyboardButton("⬅️ Назад до списку книг", callback_data="back_books")],
                [InlineKeyboardButton("⬅️ Назад до жанрів", callback_data="back_genres")]]
    text = f"*{book_name}*\n{description}\n\n💸 Ціна: {price}"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return CONFIRM_BOOK

async def confirm_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(f"{days} днів", callback_data=str(days))] for days in rental_days]
    await query.edit_message_text("Оберіть кількість днів оренди:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_RENT_DAYS

async def choose_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["days"] = query.data
    await query.edit_message_text("Введіть ваше ім'я:")
    return GET_NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    button = KeyboardButton("Поділитись номером телефону", request_contact=True)
    reply_markup = ReplyKeyboardMarkup([[button]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Будь ласка, поділіться своїм номером телефону:", reply_markup=reply_markup)
    return GET_CONTACT

async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact.phone_number if update.message.contact else update.message.text.strip()
    if not contact or len(contact) < 6:
        await update.message.reply_text("Будь ласка, введіть дійсний номер телефону або поділіться ним.")
        return GET_CONTACT

    context.user_data["contact"] = contact
    try:
        worksheet.append_row([
            context.user_data.get("location", ""),
            context.user_data.get("genre", ""),
            context.user_data.get("book", ""),
            context.user_data.get("days", ""),
            context.user_data.get("name", ""),
            contact
        ])
        logger.info("Дані успішно записано в Google Sheets")
    except Exception as e:
        logger.error("Помилка при записі у Google Sheets: %s", e)
        await update.message.reply_text("Сталася помилка при записі. Спробуйте пізніше. 🛠️")
        return ConversationHandler.END

    await update.message.reply_text("Дякуємо! Ваш запит прийнято. Очікуйте підтвердження ☕", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def back_to_genres(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await choose_location(update, context)

async def back_to_books(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await send_book_list(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Дію скасовано.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def main():
    app = Application.builder().token(os.getenv("BOT_TOKEN")).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_LOCATION: [
                CallbackQueryHandler(paginate_locations, pattern="^loc_(next|prev)_\\d+$"),
                CallbackQueryHandler(choose_location)
            ],
            CHOOSE_GENRE: [CallbackQueryHandler(choose_genre)],
            SHOW_BOOKS: [
                CallbackQueryHandler(paginate_books, pattern="^(next|prev)$"),
                CallbackQueryHandler(select_book, pattern="^book:.*")
            ],
            CONFIRM_BOOK: [
                CallbackQueryHandler(confirm_book, pattern="^confirm$"),
                CallbackQueryHandler(back_to_books, pattern="^back_books$"),
                CallbackQueryHandler(back_to_genres, pattern="^back_genres$")
            ],
            CHOOSE_RENT_DAYS: [CallbackQueryHandler(choose_days)],
            GET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            GET_CONTACT: [
                MessageHandler(filters.CONTACT, get_contact),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_contact)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)

    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", 8443)),
        webhook_url=os.getenv("WEBHOOK_URL")
    )

if __name__ == "__main__":
    main()
