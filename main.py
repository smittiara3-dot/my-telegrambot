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
book_data = {}
book_to_locations = {}
location_to_books = {}
author_to_books = {}
rental_price_map = {}


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


async def create_monopay_invoice(amount: int, description: str, order_id: str) -> str:
    url = "https://api.monobank.ua/api/merchant/invoice/create"
    headers = {
        "X-Token": MONOPAY_TOKEN,
        "Content-Type": "application/json",
    }
    data = {
        "amount": amount * 100,
        "currency": 980,
        "description": description,
        "orderId": order_id,
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
    global locations, genres, book_data, rental_price_map
    global book_to_locations, location_to_books, authors, author_to_books

    sh = gc.open_by_key(GOOGLE_SHEET_ID_LOCATIONS)
    worksheet = sh.sheet1
    records = worksheet.get_all_records()
    df = pd.DataFrame(records)

    locations = sorted(df['location'].dropna().unique().tolist())
    genres = sorted(df['genre'].dropna().unique().tolist())

    authors_raw = df['author'].dropna().unique() if 'author' in df.columns else []
    authors = sorted([a.strip() for a in authors_raw if a.strip()]) if authors_raw is not None else []

    book_data.clear()
    book_to_locations.clear()
    location_to_books.clear()
    author_to_books.clear()

    for genre in genres:
        books = []
        df_genre = df[df['genre'] == genre]
        for _, row in df_genre.iterrows():
            author = row.get('author', '').strip() if row.get('author') else ''
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

            if author:
                if author not in author_to_books:
                    author_to_books[author] = []
                if book["title"] not in [b['title'] for b in author_to_books[author]]:
                    author_to_books[author].append(book)

        book_data[genre] = books

    if not df.empty:
        rental_price_map = {
            7: int(df.iloc[0].get('price_7', 70)),
            14: int(df.iloc[0].get('price_14', 140))
        }
    else:
        rental_price_map = {7: 70, 14: 140}

    logger.info(f"Дані завантажено: {len(locations)} локацій, {len(genres)} жанрів, {len(authors)} авторів.")
    logger.info(f"Зв’язки book_to_locations: {len(book_to_locations)} книг.")
    logger.info(f"Зв’язки location_to_books: {len(location_to_books)} локацій.")
    logger.info(f"Зв’язки author_to_books: {len(author_to_books)} авторів.")


async def reload_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        load_data_from_google_sheet()
        await update.message.reply_text("Дані з Google Sheets успішно оновлено!")
        logger.info("Користувач ініціював оновлення даних з Google Sheets командою /reload")
    except Exception as e:
        logger.error(f"Помилка оновлення даних з Google Sheets: {e}", exc_info=True)
        await update.message.reply_text("Сталася помилка при оновленні даних. Спробуйте пізніше.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    try:
        load_data_from_google_sheet()
        logger.info("Дані з Google Sheets оновлені у /start")
    except Exception as e:
        logger.error(f"Помилка оновлення даних у /start: {e}")

    welcome_text = (
        "Привіт! Я — Ботик-книголюб\n"
        "Я доглядаю за Тихою поличкою — місцем, де книги говорять у тиші, а читачі знаходять саме ту історію, яка зараз потрібна.\n"
        "Я допоможу тобі обрати книгу, розповім усе, що треба знати, і проведу до затишного читання. \n"
        "Спочатку оберімо, на якій поличці ти сьогодні?\n"
        "Вибери місце, де ти знайшов(-ла) нас — і я покажу доступні книжки. Також ти можеш вибрати перелік всіх доступних або вибрати улюбленого автора."
    )

    keyboard = get_paginated_buttons(locations, 0, "location", locations_per_page, add_start_button=True)
    keyboard.append([InlineKeyboardButton("📚 Показати всі книги", callback_data="all_books")])
    keyboard.append([InlineKeyboardButton("👩‍💼 Показати всіх авторів", callback_data="all_authors")])

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
        keyboard.append([InlineKeyboardButton("👩‍💼 Показати всіх авторів", callback_data="all_authors")])
        try:
            await query.edit_message_text(
                "Спочатку оберімо, на якій поличці ти сьогодні?\n"
                "Вибери місце, де ти знайшов(-ла) нас — і я покажу доступні книжки. Також ти можеш вибрати перелік всіх доступних або вибрати улюбленого автора.",
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
        keyboard.append([InlineKeyboardButton("👩‍💼 Показати всіх авторів", callback_data="all_authors")])
        try:
            await query.edit_message_text(
                "Спочатку оберімо, на якій поличці ти сьогодні?\n"
                "Вибери місце, де ти знайшов(-ла) нас — і я покажу доступні книжки. Також ти можеш вибрати перелік всіх доступних або вибрати улюбленого автора.",
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
        await query.edit_message_text(f"На локації \"{loc_selected}\" наразі немає доступних книг.")
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
        "А тепер — трохи магії!\n"
        "Який жанр сьогодні відгукується твоєму настрою?\n"
        "Любиш щось глибоке? Може, пригодницьке? А може — спокійний нон-фікшн на вечір?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHOOSE_GENRE


async def choose_genre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    genre = query.data.split(":", 1)[1]
    loc = context.user_data.get("location", None)

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
        await query.edit_message_text("Подивимось, що тут в нас:", reply_markup=InlineKeyboardMarkup(buttons))
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
    book_title = book_hash_map.get(book_hash, None)
    if not book_title:
        try:
            await query.edit_message_text("Книгу не знайдено (некоректний код).")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return SHOW_BOOKS

    genre = context.user_data.get("genre")

    current_books = context.user_data.get("books", [])
    book = next((b for b in current_books if b["title"] == book_title), None)

    if not book:
        if genre in ["all", "all_location"] or (genre and genre.startswith("author:")):
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

    locations_for_book = book_to_locations.get(book_title, [])
    loc_text = ", ".join(locations_for_book) if locations_for_book else "Локація не вказана"

    text = (
        f"О, чудовий вибір! Ця книга — справжня перлина \n"
        f"Вона доступна на поличках: {loc_text}\n\n"
        "Вона знайшла тебе не випадково. Хай читається легко, а думки розпускаються, як чай у теплій чашці."
    )

    buttons = [
        InlineKeyboardButton("7 днів", callback_data="days:7"),
        InlineKeyboardButton("14 днів", callback_data="days:14"),
        InlineKeyboardButton("🔙 До книг", callback_data="back:books"),
        InlineKeyboardButton("🔙 До жанрів", callback_data="back:genres"),
        InlineKeyboardButton("🔙 До локацій", callback_data="back:locations"),
        InlineKeyboardButton("🏠 На початок", callback_data="back:start"),
    ]
    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([buttons]))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return BOOK_DETAILS


async def choose_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    buttons = [
        InlineKeyboardButton("7 днів", callback_data="days:7"),
        InlineKeyboardButton("14 днів", callback_data="days:14"),
        InlineKeyboardButton("🔙 До книг", callback_data="back:books"),
        InlineKeyboardButton("🔙 До жанрів", callback_data="back:genres"),
        InlineKeyboardButton("🔙 До локацій", callback_data="back:locations"),
        InlineKeyboardButton("🏠 На початок", callback_data="back:start"),
    ]
    try:
        await query.edit_message_text("Оберіть термін оренди:", reply_markup=InlineKeyboardMarkup([buttons]))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    return CHOOSE_RENT_DAYS


async def days_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    days = int(query.data.split(":")[1])
    context.user_data["days"] = str(days)

    rules_text = (
        "Перш ніж книга вирушить з тобою, розповім кілька простих і чесних правил:\n"
        "• Бронь діє 14 днів з моменту оплати\n"
        "• Книга повертається на ту ж поличку, де ти її взяв(-ла)\n"
        "• Будь ласка, читай з любовʼю, не загинай сторінки і не залишай записів\n"
        "А тепер попрошу трішки про тебе. Залиш свої прізвище та імʼя,  а також номер телефону (щоб ми могли тримати зв’язок, якщо що):"
    )
    try:
        await query.edit_message_text(rules_text)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
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

    data = context.user_data

    location = data.get("location")
    book = data.get("book", {})
    author = book.get("author", "")
    genre = data.get("genre")

    if genre is None or genre.startswith("author:") or genre in ["all", "all_location"]:
        genre_found = ""
        for g, books_list in book_data.items():
            if any(b['title'] == book.get("title") for b in books_list):
                genre_found = g
                break
        genre = genre_found
        data["genre"] = genre

    if not location:
        book_title = book.get("title", "")
        locations_list = book_to_locations.get(book_title, [])
        location = ", ".join(locations_list) if locations_list else ""
        data["location"] = location

    data["order_id"] = str(uuid.uuid4())
    data["chat_id"] = update.effective_chat.id

    days = int(data.get("days", 7))
    price_total = book.get(f'price_{days}', rental_price_map.get(days, 70))
    data["book"]["price"] = price_total

    logger.info("Отримане замовлення: %s", pprint.pformat(data))

    saved = await save_order_to_sheets(data)
    if not saved:
        await update.message.reply_text("Виникла проблема при збереженні замовлення. Спробуйте пізніше.")
        return ConversationHandler.END

    text = (
        f"📚 *Ваше замовлення:*\n"
        f"🏠 Локація: {location}\n"
        f"🖋 Автор: {author}\n"
        f"📖 Книга: {book.get('title', '')}\n"
        f"🗂 Жанр: {genre}\n"
        f"📆 Днів: {days}\n"
        f"👤 Ім'я: {data['name']}\n"
        f"📞 Контакт: {data['contact']}\n"
        f"\nСума до оплати: *{price_total} грн*"
    )
    buttons = [
        [InlineKeyboardButton("💳 Оплатити", callback_data="pay_now")],
        [InlineKeyboardButton("🏠 На початок", callback_data="back:start")],
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return CONFIRMATION


async def confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = context.user_data
    days = int(data.get("days", 7))
    price_total = data.get("book", {}).get(f"price_{days}", rental_price_map.get(days, 70))
    description = f"Оренда книги {data['book']['title']} на {days} днів"
    order_id = data["order_id"]
    try:
        invoice_url = await create_monopay_invoice(price_total, description, order_id)
        buttons = [
            [InlineKeyboardButton("Оплатити MonoPay", url=invoice_url)],
            [InlineKeyboardButton("🏠 На початок", callback_data="back:start")],
        ]
        await query.edit_message_text(
            "Будь ласка, оплатіть за посиланням нижче або поверніться в меню:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Помилка створення інвойсу MonoPay: {e}")
        buttons = [[InlineKeyboardButton("🏠 На початок", callback_data="back:start")]]
        await query.edit_message_text(
            f"Сталася помилка при створенні платежу: {e}",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    return CONFIRMATION


async def go_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "back:genres":
        if context.user_data.get("location"):
            return await show_genres_for_location(update, context)
        else:
            return await start(update, context)
    elif data == "back:books":
        return await show_books(update, context)
    elif data == "back:locations":
        welcome_text = (
            "Привіт! Я — Ботик-книголюб\n"
            "Я доглядаю за Тихою поличкою — місцем, де книги говорять у тиші, а читачі знаходять саме ту історію, яка зараз потрібна.\n"
            "Я допоможу тобі обрати книгу, розповім усе, що треба знати, і проведу до затишного читання. \n"
            "Спочатку оберімо, на якій поличці ти сьогодні?\n"
            "Вибери місце, де ти знайшов(-ла) нас — і я покажу доступні книжки. Також ти можеш вибрати перелік всіх доступних або вибрати улюбленого автора."
        )
        keyboard = get_paginated_buttons(locations, 0, "location", locations_per_page, add_start_button=True)
        keyboard.append([InlineKeyboardButton("📚 Показати всі книги", callback_data="all_books")])
        keyboard.append([InlineKeyboardButton("👩‍💼 Показати всіх авторів", callback_data="all_authors")])
        try:
            await query.edit_message_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return CHOOSE_LOCATION
    elif data == "back:start":
        context.user_data.clear()
        try:
            load_data_from_google_sheet()
            logger.info("Дані з Google Sheets оновлені при натисканні 'На початок'")
        except Exception as e:
            logger.error(f"Помилка оновлення даних при 'На початок': {e}")

        welcome_text = (
            "Привіт! Я — Ботик-книголюб\n"
            "Я доглядаю за Тихою поличкою — місцем, де книги говорять у тиші, а читачі знаходять саме ту історію, яка зараз потрібна.\n"
            "Я допоможу тобі обрати книгу, розповім усе, що треба знати, і проведу до затишного читання. \n"
            "Спочатку оберімо, на якій поличці ти сьогодні?\n"
            "Вибери місце, де ти знайшов(-ла) нас — і я покажу доступні книжки. Також ти можеш вибрати перелік всіх доступних або вибрати улюбленого автора."
        )
        keyboard = get_paginated_buttons(locations, 0, "location", locations_per_page, add_start_button=True)
        keyboard.append([InlineKeyboardButton("📚 Показати всі книги", callback_data="all_books")])
        keyboard.append([InlineKeyboardButton("👩‍💼 Показати всіх авторів", callback_data="all_authors")])
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
            await query.edit_message_text("Поки що немає доступних книг.")
            return ConversationHandler.END

        unique_books = {}
        for b in books_all:
            unique_books[b["title"]] = b

        context.user_data["books"] = list(unique_books.values())
        context.user_data["genre"] = "all"
        context.user_data["book_page"] = 0

        return await show_books(update, context)

    elif data == "all_authors":
        if not authors:
            await query.edit_message_text("Поки що немає авторів у системі.")
            return CHOOSE_LOCATION

        keyboard = get_paginated_buttons(authors, 0, "author", books_per_page, add_start_button=True)
        keyboard.append([InlineKeyboardButton("🏠 На початок", callback_data="back:start")])

        try:
            await query.edit_message_text("Оберіть автора:", reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        context.user_data["author_page"] = 0
        return CHOOSE_GENRE

    else:
        await query.answer("Невідома дія")
        return CHOOSE_LOCATION


async def choose_author(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    current_page = context.user_data.get("author_page", 0)
    max_page = (len(authors) - 1) // books_per_page

    if data == "author_next":
        next_page = min(current_page + 1, max_page)
        context.user_data["author_page"] = next_page
        keyboard = get_paginated_buttons(authors, next_page, "author", books_per_page, add_start_button=True)
        try:
            await query.edit_message_text("Оберіть автора:", reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return CHOOSE_GENRE
    elif data == "author_prev":
        prev_page = max(current_page - 1, 0)
        context.user_data["author_page"] = prev_page
        keyboard = get_paginated_buttons(authors, prev_page, "author", books_per_page, add_start_button=True)
        try:
            await query.edit_message_text("Оберіть автора:", reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return CHOOSE_GENRE
    else:
        # Виправлення логіки: спочатку обробляємо вибір автора та формуємо список книг цього автора
        author_name = data.split(":", 1)[1]
        books_by_author = author_to_books.get(author_name, [])

        if not books_by_author:
            await query.edit_message_text(f"Книг автора \"{author_name}\" наразі немає.")
            return CHOOSE_GENRE

        # Встановимо genre як "author:<author_name>" для унікальності
        context.user_data["genre"] = f"author:{author_name}"
        context.user_data["books"] = books_by_author
        context.user_data["book_page"] = 0
        context.user_data["author_name"] = author_name

        # Переходимо до показу книг (як при виборі жанру)
        return await show_books(update, context)


async def init_app():

    load_data_from_google_sheet()

    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            START_MENU: [
                CallbackQueryHandler(start_menu_handler, pattern=r"^(all_books|all_authors)$"),
            ],
            CHOOSE_LOCATION: [
                CallbackQueryHandler(choose_location, pattern=r"^location.*"),
                CallbackQueryHandler(start_menu_handler, pattern=r"^(all_books|all_authors)$"),
                CallbackQueryHandler(go_back, pattern=r"^back:(start|locations)$"),
            ],
            CHOOSE_GENRE: [
                CallbackQueryHandler(choose_genre, pattern=r"^(genre:.*|author:.*)"),
                CallbackQueryHandler(choose_author, pattern=r"^author.*"),
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
            CHOOSE_RENT_DAYS: [
            ],
            GET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            GET_CONTACT: [MessageHandler(filters.CONTACT | filters.TEXT, get_contact)],
            CONFIRMATION: [
                CallbackQueryHandler(confirm_payment, pattern=r"^pay_now$"),
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

    app.bot_updater = application

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    await application.bot.set_webhook(f"{WEBHOOK_URL.rstrip('/')}/telegram_webhook")

    logger.info(f"Server started on port {PORT}")
    logger.info(f"Telegram webhook set to {WEBHOOK_URL.rstrip('/')}/telegram_webhook")

    return app, application


async def monopay_webhook(request):
    try:
        body = await request.text()
        data = json.loads(body)
        signature = request.headers.get("X-Signature-MonoPay")
        if MONOPAY_WEBHOOK_SECRET and signature:
            computed_signature = hmac.new(MONOPAY_WEBHOOK_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(computed_signature, signature):
                logger.warning("Invalid MonoPay webhook signature")
                return web.Response(text="Invalid signature", status=403)
        order_id = data.get("orderId")
        payment_status = data.get("status")
        logger.info(f"MonoPay webhook received: orderId={order_id}, status={payment_status}")
        chat_id = await get_chat_id_for_order(order_id)
        if payment_status == "PAID" and chat_id:
            await request.app.bot_updater.bot.send_message(chat_id, f"✅ Оплата замовлення {order_id} успішна! Дякуємо за оренду ☕")
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
