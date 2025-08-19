import os
import json
import logging
import pprint
import hmac
import hashlib
import uuid
import asyncio
from aiohttp import web, ClientSession
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters, ContextTypes,
)
from telegram.error import BadRequest
import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONOPAY_TOKEN = os.getenv("MONOPAY_TOKEN")
MONOPAY_WEBHOOK_SECRET = os.getenv("MONOPAY_WEBHOOK_SECRET", None)
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
if WEBHOOK_URL:
    WEBHOOK_URL = WEBHOOK_URL.rstrip("/")
else:
    raise ValueError("WEBHOOK_URL environment variable is not set")

PORT = int(os.getenv("PORT", 8443))
GOOGLE_SHEET_ID_LOCATIONS = os.getenv("GOOGLE_SHEET_ID_LOCATIONS")
GOOGLE_SHEET_ID_ORDERS = os.getenv("GOOGLE_SHEET_ID_ORDERS")
creds_dict = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.Client(auth=credentials)
gc.session = AuthorizedSession(credentials)

(
    START_MENU,
    CHOOSE_LOCATION,
    CHOOSE_GENRE,
    SHOW_BOOKS,
    BOOK_DETAILS,
    CHOOSE_RENT_DAYS,
    GET_NAME,
    GET_CONTACT,
    CONFIRMATION,
) = range(9)

books_per_page = 10
locations_per_page = 10

locations = []
genres = []
authors = []
author_normalized_map = {}
book_data = {}
book_to_locations = {}
location_to_books = {}
author_to_books = {}
author_to_books_normalized = {}
rental_price_map = {}

def normalize_str(s: str) -> str:
    return s.strip().lower() if s else ""

def get_paginated_buttons(items, page, prefix, page_size, add_start_button=False):
    start = page * page_size
    end = min(start + page_size, len(items))
    buttons = [[InlineKeyboardButton(name, callback_data=f"{prefix}:{name}")] for name in items[start:end]]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"{prefix}_prev"))
    if end < len(items):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"{prefix}_next"))
    if nav:
        buttons.append(nav)
    if add_start_button:
        buttons.append([InlineKeyboardButton("🏠 На початок", callback_data="back:start")])
    return buttons

def make_book_callback_data(title: str) -> str:
    h = hashlib.sha256(title.encode('utf-8')).hexdigest()[:16]
    return f"book:{h}"

async def create_monopay_invoice(amount: int, description: str, invoice_id: str) -> str:
    url = "https://api.monobank.ua/api/merchant/invoice/create"
    headers = {
        "X-Token": MONOPAY_TOKEN,
        "Content-Type": "application/json",
    }
    data = {
        "amount": amount * 100,
        "currency": 980,
        "description": description,
        "invoiceid": invoice_id,
        "redirectUrl": f"{WEBHOOK_URL}/success",
        "webHookUrl": f"{WEBHOOK_URL}/monopay_callback",
    }
    async with ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as resp:
            resp_json = await resp.json()
            if resp.status == 200 and ("pageUrl" in resp_json or "invoiceUrl" in resp_json):
                return resp_json.get("pageUrl") or resp_json.get("invoiceUrl")
            else:
                logger.error(f"MonoPay invoice creation error: {resp_json}")
                raise Exception(f"Помилка створення інвойсу MonoPay: {resp_json}")

async def save_order_to_sheets(data: dict) -> bool:
    try:
        worksheet = gc.open_by_key(GOOGLE_SHEET_ID_ORDERS).sheet1
        location_str = data.get("location")
        if not location_str:
            book_title = data.get("book", {}).get("title", "")
            locs = book_to_locations.get(book_title, [])
            location_str = ", ".join(locs) if locs else ""
        book = data.get("book", {})
        author = book.get("author", "")
        order_datetime = datetime.now().isoformat(sep=' ', timespec='seconds')
        worksheet.append_row(
            [
                location_str,
                author,
                book.get("title", ""),
                data.get("genre", ""),
                data.get("days", ""),
                data.get("name", ""),
                data.get("contact", ""),
                order_datetime,
                data.get("order_id", ""),
                data.get("chat_id", ""),
                data.get("payment_status", "PAID")
            ]
        )
        return True
    except Exception as e:
        logger.error(f"Помилка запису в Google Sheets: {e}", exc_info=True)
        return False

async def get_chat_id_for_order(order_id: str) -> int | None:
    try:
        worksheet = gc.open_by_key(GOOGLE_SHEET_ID_ORDERS).sheet1
        records = worksheet.get_all_records()
        for row in records:
            if str(row.get("order_id", "")) == str(order_id):
                chat_id = row.get("chat_id")
                if chat_id:
                    return int(chat_id)
    except Exception as e:
        logger.error(f"Error getting chat_id for order: {e}")
    return None

def load_data_from_google_sheet():
    global locations, genres, authors, book_data, rental_price_map
    global book_to_locations, location_to_books, author_to_books, author_normalized_map, author_to_books_normalized
    sh = gc.open_by_key(GOOGLE_SHEET_ID_LOCATIONS)
    worksheet = sh.sheet1
    records = worksheet.get_all_records()
    df = pd.DataFrame(records)
    locations = sorted(df['location'].dropna().unique().tolist())
    genres = sorted(df['genre'].dropna().unique().tolist())
    authors.clear()
    author_normalized_map.clear()
    author_to_books.clear()
    author_to_books_normalized.clear()
    book_data.clear()
    book_to_locations.clear()
    location_to_books.clear()
    for genre in genres:
        books = []
        df_genre = df[df['genre'] == genre]
        for _, row in df_genre.iterrows():
            author_raw = row.get('author', '')
            author = author_raw.strip() if author_raw else ''
            book = {
                "title": row['title'],
                "desc": row['desc'],
                "author": author,
                "price_7": row.get('price_7', 70),
                "price_14": row.get('price_14', 140),
            }
            books.append(book)
            if book["title"] not in book_to_locations:
                book_to_locations[book["title"]] = []
            if row['location'] not in book_to_locations[book["title"]]:
                book_to_locations[book["title"]].append(row['location'])
            loc = row['location']
            if loc not in location_to_books:
                location_to_books[loc] = []
            if book["title"] not in location_to_books[loc]:
                location_to_books[loc].append(book["title"])
        book_data[genre] = books
    if not df.empty:
        row0 = df.iloc[0]
        rental_price_map = {
            7: int(row0['price_7']) if 'price_7' in row0 and pd.notna(row0['price_7']) else 70,
            14: int(row0['price_14']) if 'price_14' in row0 and pd.notna(row0['price_14']) else 140
        }
    else:
        rental_price_map = {7: 70, 14: 140}
    logger.info(f"Дані завантажено: {len(locations)} локацій, {len(genres)} жанрів.")

async def reload_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        load_data_from_google_sheet()
        await update.message.reply_text("Дані з Google Sheets успішно оновлено!")
        logger.info("Користувач ініціював оновлення даних командою /reload")
    except Exception as e:
        logger.error(f"Помилка оновлення даних: {e}", exc_info=True)
        await update.message.reply_text("Сталася помилка при оновленні даних. Спробуйте пізніше.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    try:
        load_data_from_google_sheet()
        logger.info("Дані оновлені у /start")
    except Exception as e:
        logger.error(f"Помилка оновлення даних у /start: {e}")
    welcome_text = (
            "Привіт! Я — Ботик-книголюб 📚\n"
            "Я доглядаю за Тихою поличкою — місцем, де книги говорять у тиші, а читачі знаходять саме ту історію, яка зараз потрібна\n"
            "Я допоможу тобі обрати книгу, розповім усе, що треба знати, і проведу до затишного читання 🌿\n"
            "Спочатку оберімо, на якій поличці ти сьогодні?\n"
            "Вибери місце, де ти знайшов(-ла) нас — і я покажу доступні книжки ✨\n"
    )
    keyboard = get_paginated_buttons(locations, 0, "location", locations_per_page, add_start_button=True)
    keyboard.append([InlineKeyboardButton("📚 Показати всі книги", callback_data="all_books")])
    if update.message:
        await update.message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    context.user_data["location_page"] = 0
    return CHOOSE_LOCATION

async def choose_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    current_page = context.user_data.get("location_page", 0)
    max_page = (len(locations) - 1) // locations_per_page
    if data == "location_next":
        next_page = min(current_page + 1, max_page)
        context.user_data["location_page"] = next_page
        keyboard = get_paginated_buttons(locations, next_page, "location", locations_per_page, add_start_button=True)
        keyboard.append([InlineKeyboardButton("📚 Показати всі книги", callback_data="all_books")])
        try:
            await query.edit_message_text(
                "Оберіть локацію:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return CHOOSE_LOCATION
    if data == "location_prev":
        prev_page = max(current_page - 1, 0)
        context.user_data["location_page"] = prev_page
        keyboard = get_paginated_buttons(locations, prev_page, "location", locations_per_page, add_start_button=True)
        keyboard.append([InlineKeyboardButton("📚 Показати всі книги", callback_data="all_books")])
        try:
            await query.edit_message_text(
                "Оберіть локацію:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return CHOOSE_LOCATION
    loc_selected = data.split(":", 1)[1]
    context.user_data["location"] = loc_selected
    loc_books_titles = location_to_books.get(loc_selected, [])
    if not loc_books_titles:
        await query.edit_message_text(f"На локації \"{loc_selected}\" немає доступних книг.")
        return CHOOSE_LOCATION
    genres_in_location_set = set()
    for genre, books in book_data.items():
        titles = [b['title'] for b in books]
        for t in loc_books_titles:
            if t in titles:
                genres_in_location_set.add(genre)
    genres_in_location = sorted(genres_in_location_set)
    context.user_data["location_genres"] = genres_in_location
    context.user_data["location_books"] = loc_books_titles
    await show_genres_for_location(update, context)
    return CHOOSE_GENRE

async def show_genres_for_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    genres_loc = context.user_data.get("location_genres", [])
    loc = context.user_data.get("location", "")
    if not genres_loc:
        await query.edit_message_text(f"На локації \"{loc}\" немає доступних жанрів.")
        return CHOOSE_LOCATION
    keyboard = [[InlineKeyboardButton(genre, callback_data=f"genre:{genre}")] for genre in genres_loc]
    keyboard.append([InlineKeyboardButton("📚 Показати всі книги на локації", callback_data="genre:all_location")])
    keyboard.append(
        [InlineKeyboardButton("🔙 Назад до локацій", callback_data="back:locations"),
         InlineKeyboardButton("🏠 На початок", callback_data="back:start")]
    )
    await query.edit_message_text(
        "А тепер — трохи магії! Який жанр сьогодні відгукується твоєму настрою?\n\n"
        "Любиш щось глибоке? Може, пригодницьке? А може — спокійний нон-фікшн на вечір?\n",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHOOSE_GENRE

async def choose_genre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    genre = query.data.split(":", 1)[1]
    loc = context.user_data.get("location")
    if genre == "all_location":
        loc_book_titles = context.user_data.get("location_books", [])
        if not loc_book_titles:
            await query.edit_message_text(f"На локації \"{loc}\" немає доступних книг.")
            return ConversationHandler.END
        books_list = []
        added_titles = set()
        for genre_books in book_data.values():
            for b in genre_books:
                if b["title"] in loc_book_titles and b["title"] not in added_titles:
                    books_list.append(b)
                    added_titles.add(b["title"])
        if not books_list:
            await query.edit_message_text(f"На локації \"{loc}\" немає доступних книг.")
            return ConversationHandler.END
        context.user_data["genre"] = "all_location"
        context.user_data["books"] = books_list
        context.user_data["book_page"] = 0
        await show_books(update, context)
        return SHOW_BOOKS
    if loc:
        loc_books_titles = location_to_books.get(loc, [])
        genre_books = book_data.get(genre, [])
        filtered_books = [b for b in genre_books if b["title"] in loc_books_titles]
        if not filtered_books:
            try:
                await query.edit_message_text("Немає книг у цьому жанрі на цій локації.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END
        context.user_data["genre"] = genre
        context.user_data["books"] = filtered_books
        context.user_data["book_page"] = 0
        await show_books(update, context)
        return SHOW_BOOKS
    else:
        genre_books = book_data.get(genre, [])
        if not genre_books:
            try:
                await query.edit_message_text("Немає книг у цьому жанрі.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            return ConversationHandler.END
        context.user_data["genre"] = genre
        context.user_data["books"] = genre_books
        context.user_data["book_page"] = 0
        await show_books(update, context)
        return SHOW_BOOKS

async def show_books(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    books = context.user_data.get("books", [])
    page = context.user_data.get("book_page", 0)
    start, end = page * books_per_page, (page + 1) * books_per_page
    page_books = books[start:end]
    buttons = []
    book_hash_map = {}
    for book in page_books:
        book_title = book['title']
        h = hashlib.sha256(book_title.encode('utf-8')).hexdigest()[:16]
        book_hash_map[h] = book_title
        author = book.get("author", "")
        title_text = f"{book_title}"
        if author:
            title_text += f" ({author})"
        buttons.append([InlineKeyboardButton(title_text, callback_data=f"book:{h}")])
    context.user_data["book_hash_map"] = book_hash_map
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data="book_prev"))
    if end < len(books):
        nav.append(InlineKeyboardButton("➡️", callback_data="book_next"))
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton("🔙 До жанрів", callback_data="back:genres"),
            InlineKeyboardButton("🔙 До локацій", callback_data="back:locations"),
            InlineKeyboardButton("🏠 На початок", callback_data="back:start"),
        ]
    )
    try:
        await query.edit_message_text("Подивимось, що тут у нас:", reply_markup=InlineKeyboardMarkup(buttons))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return SHOW_BOOKS

async def book_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    current_page = context.user_data.get("book_page", 0)
    books = context.user_data.get("books", [])
    max_page = (len(books) - 1) // books_per_page if books else 0
    if query.data == "book_next":
        context.user_data["book_page"] = min(current_page + 1, max_page)
    elif query.data == "book_prev":
        context.user_data["book_page"] = max(current_page - 1, 0)
    return await show_books(update, context)

async def book_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    book_hash = query.data.split(":", 1)[1]
    book_hash_map = context.user_data.get("book_hash_map", {})
    book_title = book_hash_map.get(book_hash)
    if not book_title:
        try:
            await query.edit_message_text("Книгу не знайдено (неправильний код).")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return SHOW_BOOKS
    genre = context.user_data.get("genre", "")
    current_books = context.user_data.get("books", [])
    book = next((b for b in current_books if b["title"] == book_title), None)
    if not book:
        if genre in ["all", "all_location"]:
            for g_books in book_data.values():
                candidate = next((b for b in g_books if b["title"] == book_title), None)
                if candidate:
                    book = candidate
                    break
        else:
            genre_books = book_data.get(genre, [])
            book = next((b for b in genre_books if b["title"] == book_title), None)
    if not book:
        try:
            await query.edit_message_text("Книгу не знайдено.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return SHOW_BOOKS
    context.user_data["book"] = book
    author = book.get("author", "Невідомий автор")
    title = book.get("title", "Без назви")
    desc = book.get("desc", "Опис відсутній")
    book_genre = context.user_data.get("genre", "Жанр не вказано")
    book_info = f"Автор: {author}\nНазва: {title}\nЖанр: {book_genre}\nОпис: {desc}\n\n"
    await query.edit_message_text(
        "О, чудовий вибір! Ця книга — справжня перлина 🌼\n\n"
        + book_info +
        "Вона знайшла тебе не випадково. Хай читається легко, а думки розпускаються, як чай у теплій чашці.\n\n"
        "А тепер попрошу трішки про тебе. Залиш свої прізвище та імʼя,  а також номер телефону (щоб ми могли тримати зв’язок, якщо що)\n\n"
        "Будь ласка, введіть своє ім’я для оформлення замовлення:"
    )
    return GET_NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    button = KeyboardButton("📱 Поділитися номером", request_contact=True)
    reply_markup = ReplyKeyboardMarkup([[button]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Надішліть номер телефону:", reply_markup=reply_markup)
    return GET_CONTACT

async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact.phone_number if update.message.contact else update.message.text.strip()
    context.user_data["contact"] = contact
    buttons = [
        [InlineKeyboardButton("7 днів", callback_data="days:7")],
        [InlineKeyboardButton("14 днів", callback_data="days:14")],
        [InlineKeyboardButton("🏠 На початок", callback_data="back:start")],
    ]
    await update.message.reply_text(
        "Перш ніж книга вирушить з тобою, розповім кілька простих і чесних правил: бронь діє 7 чи 14 днів з моменту оплати\n\n"
        "Книга повертається на ту ж поличку, де ти її взяв(-ла) Будь ласка, читай з любовʼю, не загинай сторінки і не залишай записів\n\n"
        "Оберіть термін оренди:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return BOOK_DETAILS

async def days_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    days = int(query.data.split(":")[1])
    context.user_data["days"] = str(days)
    data = context.user_data
    location = data.get("location")
    book = data.get("book", {})
    author = book.get("author", "")
    genre = data.get("genre")
    if not location:
        book_title = book.get("title", "")
        locations_list = book_to_locations.get(book_title, [])
        location = ", ".join(locations_list) if locations_list else ""
        data["location"] = location
    data["order_id"] = str(uuid.uuid4())
    data["chat_id"] = query.message.chat.id
    price_total = book.get(f'price_{days}', rental_price_map.get(days, 70))
    data["book"]["price"] = price_total
    data["payment_status"] = "PAID"  # Додано, щоб зберегти статус платежу
    logger.info("Отримане замовлення: %s", pprint.pformat(data))
    saved = await save_order_to_sheets(data)
    if not saved:
        await query.edit_message_text("Проблема із збереженням замовлення. Спробуйте пізніше.")
        return ConversationHandler.END
    description = f"Оренда книги {data['book']['title']} на {days} днів"
    try:
        invoice_url = await create_monopay_invoice(price_total, description, data["order_id"])
        buttons = [
            [InlineKeyboardButton("💳 Оплатити MonoPay", url=invoice_url)],
            [InlineKeyboardButton("🏠 На початок", callback_data="back:start")],
        ]
        text = (
            f"📚 Ваше замовлення:\n"
            f"🏠 Локація: {location}\n"
            f"🖋 Автор: {author}\n"
            f"📖 Книга: {book.get('title')}\n"
            f"🗂 Жанр: {genre}\n"
            f"📆 Днів: {days}\n"
            f"👤 Ім'я: {data.get('name', 'не вказано')}\n"
            f"📞 Контакт: {data.get('contact', 'не вказано')}\n"
            f"\nСума до оплати: <b>{price_total} грн</b>\n\n"
            f"Натисніть кнопку нижче, щоб оплатити."
        )
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Помилка створення інвойсу MonoPay: {e}")
        buttons = [[InlineKeyboardButton("🏠 На початок", callback_data="back:start")]]
        await query.edit_message_text(f"Помилка при створенні платежу: {e}", reply_markup=InlineKeyboardMarkup(buttons))
        return ConversationHandler.END
    return CONFIRMATION

async def monopay_webhook(request):
    try:
        body = await request.text()
        data = json.loads(body)
        signature = request.headers.get("X-Signature-MonoPay")
        if MONOPAY_WEBHOOK_SECRET and signature:
            computed_signature = hmac.new(
                MONOPAY_WEBHOOK_SECRET.encode(),
                body.encode(),
                hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(computed_signature, signature):
                logger.warning("Invalid MonoPay webhook signature")
                return web.Response(text="Invalid signature", status=403)
        invoiceId = data.get("invoiceId")
        payment_status = data.get("status")
        logger.info(f"MonoPay webhook received: invoiceId={invoiceId}, status={payment_status}")
        if payment_status == "PAID":
            chat_id = await get_chat_id_for_order(invoiceId)
            if chat_id:
                text = "✅ Все готово! Обійми книжку, забери її з полички — і насолоджуйся кожною сторінкою.\nНехай ця історія буде саме тією, яку тобі зараз потрібно.\nЗ любов’ю до читання,Тиха поличка і я — Ботик-книголюб 🤍"
                buttons = [
                    [InlineKeyboardButton("🏠 На початок", callback_data="back:start")]
                ]
                try:
                    await request.app.bot_updater.bot.send_message(
                        chat_id,
                        text,
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )
                except Exception as e:
                    logger.error(f"Не вдалося надіслати повідомлення в Telegram: {e}")
            else:
                logger.warning(f"Chat ID for order {invoiceId} not found")
        return web.Response(text="OK")
    except Exception as e:
        logger.exception("Error in MonoPay webhook:")
        return web.Response(text=f"Error: {e}", status=500)

async def telegram_webhook_handler(request):
    app = request.app
    bot_app = app.bot_updater
    body = await request.text()
    update = Update.de_json(json.loads(body), bot_app.bot)
    await bot_app.process_update(update)
    return web.Response(text="OK", status=200)

async def success_page_handler(request):
    html_content = f"""
    <!DOCTYPE html>
    <html lang="uk">
    <head>
        <meta charset="UTF-8" />
        <title>Оплата успішна</title>
        <script>
            function goToBot() {{
                window.location.href = "https://t.me/{os.getenv('BOT_USERNAME', '').lstrip('@')}";
            }}
            setTimeout(goToBot, 5000);
        </script>
        <style>
            body {{
                font-family: Arial, sans-serif;
                text-align: center;
                margin-top: 50px;
            }}
            a.button {{
                display: inline-block;
                padding: 10px 20px;
                font-size: 18px;
                color: #fff;
                background-color: #0088cc;
                text-decoration: none;
                border-radius: 5px;
                margin-top: 20px;
            }}
        </style>
    </head>
    <body>
        <h1>Оплата успішна! Дякуємо за оренду.</h1>
        <p>Через 5 секунд ви автоматично перейдете в бот.</p>
        <p>
            <a href="https://t.me/{os.getenv('BOT_USERNAME', '').lstrip('@')}" class="button">Повернутися в бот зараз</a>
        </p>
    </body>
    </html>
    """
    return web.Response(text=html_content, content_type='text/html')

async def go_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    if data == "back:genres":
        if context.user_data.get("location"):
            return await show_genres_for_location(update, context)
        else:
            return await start(update, context)
    if data == "back:books":
        return await show_books(update, context)
    if data == "back:locations":
        welcome_text = (
            "Привіт! Я — Ботик-книголюб 📚\n"
            "Я доглядаю за Тихою поличкою — місцем, де книги говорять у тиші, а читачі знаходять саме ту історію, яка зараз потрібна\n"
            "Я допоможу тобі обрати книгу, розповім усе, що треба знати, і проведу до затишного читання 🌿\n\n"
            "Спочатку оберімо, на якій поличці ти сьогодні?\n"
            "Вибери місце, де ти знайшов(-ла) нас — і я покажу доступні книжки ✨\n"
        )
        keyboard = get_paginated_buttons(locations, 0, "location", locations_per_page, add_start_button=True)
        keyboard.append([InlineKeyboardButton("📚 Показати всі книги", callback_data="all_books")])
        try:
            await query.edit_message_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return CHOOSE_LOCATION
    if data == "back:start":
        context.user_data.clear()
        try:
            load_data_from_google_sheet()
        except Exception as e:
            logger.error(f"Помилка оновлення даних при 'На початок': {e}")
        welcome_text = (
            "Привіт! Я — Ботик-книголюб\n"
            "Почнемо спочатку.\n"
            "Виберіть локацію або книгу."
        )
        keyboard = get_paginated_buttons(locations, 0, "location", locations_per_page, add_start_button=True)
        keyboard.append([InlineKeyboardButton("📚 Показати всі книги", callback_data="all_books")])
        try:
            await query.edit_message_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return CHOOSE_LOCATION

async def start_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "all_books":
        books_all = []
        for genre_books in book_data.values():
            books_all.extend(genre_books)
        if not books_all:
            await query.edit_message_text("Немає доступних книг.")
            return ConversationHandler.END
        unique_books = {}
        for b in books_all:
            unique_books[b["title"]] = b
        context.user_data["books"] = list(unique_books.values())
        context.user_data["genre"] = "all"
        context.user_data["book_page"] = 0
        return await show_books(update, context)
    await query.answer("Невідома дія")
    return CHOOSE_LOCATION

async def init_app():
    load_data_from_google_sheet()
    application = Application.builder().token(BOT_TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            START_MENU: [
                CallbackQueryHandler(start_menu_handler, pattern=r"^(all_books)$"),
            ],
            CHOOSE_LOCATION: [
                CallbackQueryHandler(choose_location, pattern=r"^location.*"),
                CallbackQueryHandler(start_menu_handler, pattern=r"^(all_books)$"),
                CallbackQueryHandler(go_back, pattern=r"^back:(start|locations)$"),
            ],
            CHOOSE_GENRE: [
                CallbackQueryHandler(choose_genre, pattern=r"^genre:.*"),
                CallbackQueryHandler(go_back, pattern=r"^back:(locations|start|genres)$"),
            ],
            SHOW_BOOKS: [
                CallbackQueryHandler(book_navigation, pattern=r"^book_(next|prev)$"),
                CallbackQueryHandler(book_detail, pattern=r"^book:.*"),
                CallbackQueryHandler(go_back, pattern=r"^back:(genres|locations|start)$"),
            ],
            BOOK_DETAILS: [
                CallbackQueryHandler(days_chosen, pattern=r"^days:\d+$"),
                CallbackQueryHandler(go_back, pattern=r"^back:(books|genres|locations|start)$"),
            ],
            CHOOSE_RENT_DAYS: [],
            GET_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)
            ],
            GET_CONTACT: [
                MessageHandler(filters.CONTACT | filters.TEXT, get_contact)
            ],
            CONFIRMATION: [
                CallbackQueryHandler(go_back, pattern=r"^back:start$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda update, context: update.message.reply_text("❌ Скасовано."))],
    )
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reload", reload_data))
    await application.initialize()
    await application.start()
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="OK", status=200))
    app.router.add_post("/telegram_webhook", telegram_webhook_handler)
    app.router.add_post("/monopay_callback", monopay_webhook)
    app.router.add_get("/success", success_page_handler)
    app.bot_updater = application
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    await application.bot.set_webhook(f"{WEBHOOK_URL}/telegram_webhook")
    logger.info(f"Server started on port {PORT}")
    logger.info(f"Telegram webhook set to {WEBHOOK_URL}/telegram_webhook")
    return app, application

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app, application = loop.run_until_complete(init_app())
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        loop.run_until_complete(application.stop())
        loop.run_until_complete(application.shutdown())
