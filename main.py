import logging
import telegram
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Налаштування логування
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Авторизація Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("creds.json", scope)
client = gspread.authorize(creds)
sheet = client.open("RentalBookBot").sheet1
worksheet = sheet

# Стан розмови
CHOOSE_LOCATION, CHOOSE_GENRE, SHOW_BOOKS, CHOOSE_RENT_DAYS, GET_NAME, GET_CONTACT = range(6)

# Дані
genres = {
    "Фантастика": ["Дюна", "1984", "451 градус за Фаренгейтом", "Пікнік на узбіччі"],
    "Романтика": ["Гордість і упередження", "Коханець леді Чаттерлей", "Тихий Дон"],
    "Детектив": ["Шерлок Холмс", "Десять негренят", "Дівчина у потягу"],
}

book_info = {
    "Дюна": {"desc": "Науково-фантастичний роман Френка Герберта.", "price": "50 грн"},
    "1984": {"desc": "Антиутопія Джорджа Орвелла.", "price": "45 грн"},
    "451 градус за Фаренгейтом": {"desc": "Книга про майбутнє, де заборонено читати книги.", "price": "40 грн"},
    "Пікнік на узбіччі": {"desc": "Повість братів Стругацьких про зону аномалій.", "price": "55 грн"},
    "Гордість і упередження": {"desc": "Класичний роман Джейн Остін.", "price": "35 грн"},
    "Коханець леді Чаттерлей": {"desc": "Скандальний роман Девіда Лоуренса.", "price": "50 грн"},
    "Тихий Дон": {"desc": "Епічний роман Михайла Шолохова.", "price": "60 грн"},
    "Шерлок Холмс": {"desc": "Класичні детективи Артура Конан Дойля.", "price": "30 грн"},
    "Десять негренят": {"desc": "Детективна історія Агати Крісті.", "price": "35 грн"},
    "Дівчина у потягу": {"desc": "Психологічний трилер Поли Гоукінз.", "price": "40 грн"},
}

locations = [f"Кав'ярня №{i}" for i in range(1, 21)]
locations_per_page = 10
books_per_page = 10

# Обробники
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("/start від %s", update.effective_user.first_name)
    context.user_data["location_page"] = 0
    text = (
        "👋 *Вас вітає Тиха Поличка!*

"
        "Сучасний і зручний спосіб оренди книжок у затишних для вас місцях.
"
        "Будь ласка, оберіть заклад:"
    )
    return await send_location_list(update, context, new_message=True, intro_text=text)

async def send_location_list(update: Update, context: ContextTypes.DEFAULT_TYPE, new_message=False, intro_text=None):
    page = context.user_data.get("location_page", 0)
    start = page * locations_per_page
    end = start + locations_per_page
    page_locations = locations[start:end]

    keyboard = [[InlineKeyboardButton(loc, callback_data=loc)] for loc in page_locations]
    nav_buttons = []
    if start > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data="loc_prev"))
    if end < len(locations):
        nav_buttons.append(InlineKeyboardButton("➡️ Далі", callback_data="loc_next"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    if new_message:
        await update.message.reply_text(intro_text or "Оберіть локацію:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(intro_text or "Оберіть локацію:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return CHOOSE_LOCATION

async def paginate_locations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "loc_next":
        context.user_data["location_page"] += 1
    elif query.data == "loc_prev":
        context.user_data["location_page"] -= 1
    return await send_location_list(update, context)

async def choose_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["location"] = query.data
    keyboard = [[InlineKeyboardButton(genre, callback_data=genre)] for genre in genres]
    await query.edit_message_text("Оберіть жанр:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_GENRE

async def choose_genre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["genre"] = query.data
    context.user_data["book_page"] = 0
    return await show_books(update, context)

async def show_books(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = context.user_data.get("book_page", 0)
    genre = context.user_data.get("genre", "")
    book_list = genres.get(genre, [])
    start = page * books_per_page
    end = start + books_per_page
    page_books = book_list[start:end]

    keyboard = [[InlineKeyboardButton(book, callback_data=book)] for book in page_books]
    nav_buttons = []
    if start > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data="prev_books"))
    if end < len(book_list):
        nav_buttons.append(InlineKeyboardButton("➡️ Далі", callback_data="next_books"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    await update.callback_query.edit_message_text("Оберіть книгу:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SHOW_BOOKS

async def paginate_books(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "next_books":
        context.user_data["book_page"] += 1
    elif query.data == "prev_books":
        context.user_data["book_page"] -= 1
    return await show_books(update, context)

async def select_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["book"] = query.data
    book_title = context.user_data["book"]
    info = book_info.get(book_title, {"desc": "Опис недоступний.", "price": "Невідомо"})

    keyboard = [
        [InlineKeyboardButton("✅ Підтвердити оренду", callback_data="confirm_rent")],
        [InlineKeyboardButton("⬅️ Назад до списку книг", callback_data="back_to_books")],
        [InlineKeyboardButton("🔙 Назад до жанрів", callback_data="back_to_genres")]
    ]

    text = f"*{book_title}*

📝 {info['desc']}
💰 Оренда: {info['price']}

Підтверджуєте оренду?"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return "BOOK_CONFIRM"

async def choose_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["days"] = query.data
    await query.edit_message_text("👤 Введіть ваше ім'я:")
    return "GET_NAME"

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name or len(name) < 2:
        await update.message.reply_text("Будь ласка, введіть дійсне ім'я.")
        return "GET_NAME"
    context.user_data["name"] = name
    keyboard = [[InlineKeyboardButton("📱 Поділитися номером", callback_data="request_contact")]]
    await update.message.reply_text("Будь ласка, натисніть кнопку нижче, щоб поділитися номером телефону:", reply_markup=InlineKeyboardMarkup(keyboard))
    return "GET_CONTACT"

async def request_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[KeyboardButton("📲 Надіслати номер", request_contact=True)]]
    await query.message.reply_text("Натисніть кнопку нижче, щоб поділитися своїм номером:", reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
    return GET_CONTACT

async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.contact:
        contact = update.message.contact.phone_number
    else:
        contact = update.message.text.strip()
    if not contact or len(contact) < 6:
        await update.message.reply_text("Будь ласка, введіть дійсний номер телефону.")
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
    except Exception as e:
        await update.message.reply_text("Сталася помилка при записі. Спробуйте пізніше.")
        return ConversationHandler.END
    await update.message.reply_text("✅ Дякуємо! Ваш запит прийнято. Очікуйте підтвердження ☕", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def main():
    app = Application.builder().token("BOT_TOKEN").build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_LOCATION: [
                CallbackQueryHandler(paginate_locations, pattern="^loc_(next|prev)$"),
                CallbackQueryHandler(choose_location),
            ],
            CHOOSE_GENRE: [CallbackQueryHandler(choose_genre)],
            SHOW_BOOKS: [
                CallbackQueryHandler(paginate_books, pattern="^(next_books|prev_books)$"),
                CallbackQueryHandler(select_book),
            ],
            "BOOK_CONFIRM": [
                CallbackQueryHandler(choose_genre, pattern="^back_to_genres$"),
                CallbackQueryHandler(paginate_books, pattern="^back_to_books$"),
                CallbackQueryHandler(choose_days, pattern="^confirm_rent$")
            ],
            CHOOSE_RENT_DAYS: [CallbackQueryHandler(choose_days)],
            "GET_NAME": [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            "GET_CONTACT": [
                CallbackQueryHandler(request_contact, pattern="^request_contact$"),
                MessageHandler(filters.CONTACT | filters.TEXT & ~filters.COMMAND, get_contact)
            ],
        },
        fallbacks=[],
    )

    app.add_handler(conv_handler)
    app.run_polling()

if __name__ == "__main__":
    main()
