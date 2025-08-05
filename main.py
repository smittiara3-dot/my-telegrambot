import os
import json
import logging
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove, KeyboardButton, ReplyKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters, ContextTypes,
)
import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession

load_dotenv()

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Стани
CHOOSE_LOCATION, CHOOSE_GENRE, SHOW_BOOKS, BOOK_DETAILS, CHOOSE_RENT_DAYS, GET_NAME, GET_CONTACT, CONFIRM_ORDER = range(8)

# Дані для бота
locations = [f"Кав'ярня {chr(65+i)}" for i in range(20)]  # 20 локацій

genres = ["Фантастика", "Роман", "Історія", "Детектив"]

books = {
    "Фантастика": [
        ("Дюна", "Епічний науково-фантастичний роман про пустельну планету та боротьбу за виживання.", 120),
        ("1984", "Антиутопія про тоталітарний режим і контроль над свідомістю.", 100),
        ("Фундація", "Історія занепаду Галактичної імперії і наукового передбачення.", 110),
        ("Зоряний десант", "Мілітаристична фантастика з глибокими соціальними темами.", 105),
        ("Марсіанин", "Астронавт намагається вижити на Марсі після аварії.", 95),
        ("Темна матерія", "Історія альтернативних реальностей і вибору.", 115),
        ("Гіперіон", "Космічна опера з глибокими філософськими ідеями.", 130),
        ("Пісня льоду й полум'я", "Фентезі з елементами політичної гри.", 140),
        ("Машина часу", "Класична історія подорожей у часі.", 85),
        ("Космічна одіссея 2001", "Глибока розповідь про людське походження і технології.", 125),
        ("Артеміда", "Кримінальна історія на Місяці.", 90),
    ],
    # Інші жанри за аналогією...
    "Роман": [("Анна Кареніна", "Трагічна історія кохання та моральних конфліктів.", 100)],
    "Історія": [("Історія України", "Огляд ключових подій української історії.", 90)],
    "Детектив": [("Шерлок Холмс", "Класичні детективні історії.", 80)],
}

rental_days = [10, 14, 21, 30]
books_per_page = 10
locations_per_page = 10

SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
json_creds = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
creds_dict = json.loads(json_creds)
credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.Client(auth=credentials)
gc.session = AuthorizedSession(credentials)
sh = gc.open("RentalBookBot")
worksheet = sh.sheet1

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Вас вітає Тиха Поличка!*\nСучасний і зручний спосіб оренди книжок у затишних для вас місцях.",
        parse_mode="Markdown"
    )
    context.user_data['location_page'] = 0
    return await show_locations(update, context)

async def show_locations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = context.user_data.get("location_page", 0)
    start = page * locations_per_page
    end = start + locations_per_page
    page_locs = locations[start:end]
    keyboard = [[InlineKeyboardButton(loc, callback_data=loc)] for loc in page_locs]
    nav_buttons = []
    if start > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data="loc_prev"))
    if end < len(locations):
        nav_buttons.append(InlineKeyboardButton("➡️ Далі", callback_data="loc_next"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    if update.message:
        await update.message.reply_text("Оберіть локацію:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.callback_query.edit_message_text("Оберіть локацію:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_LOCATION

async def location_pagination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "loc_next":
        context.user_data["location_page"] += 1
    else:
        context.user_data["location_page"] -= 1
    return await show_locations(update, context)

async def choose_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["location"] = query.data
    keyboard = [[InlineKeyboardButton(genre, callback_data=genre)] for genre in genres]
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_locations")])
    await query.edit_message_text("Оберіть жанр:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_GENRE

async def genre_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "back_to_locations":
        return await show_locations(update, context)

    context.user_data["genre"] = query.data
    context.user_data["page"] = 0
    context.user_data["book_list"] = books.get(query.data, [])
    return await show_books(update, context)

async def show_books(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    book_list = context.user_data["book_list"]
    page = context.user_data["page"]
    start = page * books_per_page
    end = start + books_per_page
    page_books = book_list[start:end]
    keyboard = [[InlineKeyboardButton(b[0], callback_data=f"book:{b[0]}")] for b in page_books]
    nav_buttons = []
    if start > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data="prev_books"))
    if end < len(book_list):
        nav_buttons.append(InlineKeyboardButton("➡️ Далі", callback_data="next_books"))
    keyboard.append(nav_buttons)
    keyboard.append([InlineKeyboardButton("⬅️ Назад до жанрів", callback_data="back_to_genres")])
    await query.edit_message_text("Оберіть книгу:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SHOW_BOOKS

async def paginate_books(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "next_books":
        context.user_data["page"] += 1
    elif query.data == "prev_books":
        context.user_data["page"] -= 1
    elif query.data == "back_to_genres":
        return await choose_location(update, context)
    return await show_books(update, context)

async def show_book_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    book_name = query.data.split(":")[1]
    genre = context.user_data["genre"]
    for b in books[genre]:
        if b[0] == book_name:
            context.user_data["book"] = book_name
            context.user_data["description"] = b[1]
            context.user_data["price"] = b[2]
            break

    keyboard = [
        [InlineKeyboardButton("✅ Орендувати", callback_data="rent")],
        [InlineKeyboardButton("⬅️ Назад до книг", callback_data="back_to_books")],
        [InlineKeyboardButton("⬅️ Назад до жанрів", callback_data="back_to_genres")],
    ]
    text = f"📖 *{book_name}*\n{context.user_data['description']}\n💰 *Ціна оренди*: {context.user_data['price']} грн"
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    return BOOK_DETAILS

async def confirm_rent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(f"{d} днів", callback_data=str(d))] for d in rental_days]
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
    button = KeyboardButton("📱 Поділитися номером телефону", request_contact=True)
    reply_markup = ReplyKeyboardMarkup([[button]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Надішліть ваш номер телефону:", reply_markup=reply_markup)
    return GET_CONTACT

async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact.phone_number if update.message.contact else update.message.text
    context.user_data["contact"] = contact
    try:
        worksheet.append_row([
            context.user_data.get("name"),
            context.user_data.get("contact"),
            context.user_data.get("location"),
            context.user_data.get("genre"),
            context.user_data.get("book"),
            context.user_data.get("days"),
            str(context.user_data.get("price"))
        ])
    except Exception as e:
        logger.error("Помилка при записі у Google Sheets: %s", e)
        await update.message.reply_text("Сталася помилка. Спробуйте пізніше.")
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton("💳 Оплатити", callback_data="pay")]]
    summary = (
        f"✅ Замовлення оформлено!\n\n"
        f"👤 Ім'я: {context.user_data['name']}\n"
        f"📞 Контакт: {context.user_data['contact']}\n"
        f"📍 Локація: {context.user_data['location']}\n"
        f"📚 Жанр: {context.user_data['genre']}\n"
        f"📖 Книга: {context.user_data['book']}\n"
        f"📅 Днів: {context.user_data['days']}\n"
        f"💸 Сума: {context.user_data['price']} грн"
    )
    await update.message.reply_text(summary, reply_markup=InlineKeyboardMarkup(keyboard))
    return CONFIRM_ORDER

async def finish_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🎉 Дякуємо за замовлення! Раді, що ви обрали Тиху Поличку ☕")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Дію скасовано.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def main():
    app = Application.builder().token(os.getenv("BOT_TOKEN")).build()
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_LOCATION: [
                CallbackQueryHandler(location_pagination, pattern="^loc_(prev|next)$"),
                CallbackQueryHandler(choose_location),
            ],
            CHOOSE_GENRE: [CallbackQueryHandler(genre_selection)],
            SHOW_BOOKS: [
                CallbackQueryHandler(paginate_books, pattern="^(next_books|prev_books|back_to_genres)$"),
                CallbackQueryHandler(show_book_details, pattern="^book:.*"),
            ],
            BOOK_DETAILS: [
                CallbackQueryHandler(confirm_rent, pattern="^rent$"),
                CallbackQueryHandler(show_books, pattern="^back_to_books$"),
                CallbackQueryHandler(choose_location, pattern="^back_to_genres$"),
            ],
            CHOOSE_RENT_DAYS: [CallbackQueryHandler(choose_days)],
            GET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            GET_CONTACT: [MessageHandler(filters.CONTACT | (filters.TEXT & ~filters.COMMAND), get_contact)],
            CONFIRM_ORDER: [CallbackQueryHandler(finish_payment, pattern="^pay$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_handler)
    app.run_webhook(listen="0.0.0.0", port=int(os.getenv("PORT", 8443)), webhook_url=os.getenv("WEBHOOK_URL"))

if __name__ == "__main__":
    main()
