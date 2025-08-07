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

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONOPAY_TOKEN = os.getenv("MONOPAY_TOKEN")
MONOPAY_WEBHOOK_SECRET = os.getenv("MONOPAY_WEBHOOK_SECRET", None)
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # без кінцевого слеша
PORT = int(os.getenv("PORT", 8443))

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
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
    DEPOSIT_PAYMENT,
    CHOOSE_LOCATION,
    CHOOSE_GENRE,
    SHOW_BOOKS,
    BOOK_DETAILS,
    CHOOSE_RENT_DAYS,
    GET_NAME,
    GET_CONTACT,
    CONFIRMATION,
) = range(10)

books_per_page = 10
locations_per_page = 10

locations = []
genres = []
book_data = {}
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
        worksheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
        worksheet.append_row(
            [
                data.get("location", ""),
                data.get("genre", ""),
                data.get("book", {}).get("title", ""),
                data.get("days", ""),
                data.get("name", ""),
                data.get("contact", ""),
                data.get("order_id", ""),
                data.get("chat_id", ""),
            ]
        )
        return True
    except Exception as e:
        logger.error(f"Помилка запису в Google Sheets: {e}", exc_info=True)
        return False


async def get_chat_id_for_order(order_id: str) -> int | None:
    try:
        worksheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
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
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    worksheet = sh.sheet1
    records = worksheet.get_all_records()
    df = pd.DataFrame(records)
    # Унікальні локації і жанри
    locations = sorted(df['location'].dropna().unique().tolist())
    genres = sorted(df['genre'].dropna().unique().tolist())
    book_data.clear()
    for genre in genres:
        books = []
        df_genre = df[df['genre'] == genre]
        for _, row in df_genre.iterrows():
            book = {
                "title": row['title'],
                "desc": row['desc'],
                "price_7": row.get('price_7', 70),
                "price_14": row.get('price_14', 140),
            }
            books.append(book)
        book_data[genre] = books
    if not df.empty:
        rental_price_map = {
            7: int(df.iloc[0].get('price_7', 70)),
            14: int(df.iloc[0].get('price_14', 140))
        }
    else:
        rental_price_map = {7: 70, 14: 140}
    logger.info(f"Дані завантажено: {len(locations)} локацій, {len(genres)} жанрів.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обробник команди /start, яка працює в будь-який момент,
    скидає стан користувача і показує початкове меню.
    """
    context.user_data.clear()
    keyboard = [
        [
            InlineKeyboardButton("Я новий клієнт", callback_data="start:new_client"),
            InlineKeyboardButton("Я вже користуюсь сервісом", callback_data="start:existing_client"),
        ]
    ]
    if update.message:
        await update.message.reply_text(
            "Вітаємо! Оберіть, будь ласка, варіант:", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                "Вітаємо! Оберіть, будь ласка, варіант:", reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    return START_MENU


async def start_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "start:new_client":
        deposit_amount = 500
        order_id = f"deposit_{uuid.uuid4()}"
        context.user_data["deposit_order_id"] = order_id
        context.user_data["is_new_client"] = True

        description = "Застава за користування Тихою Поличкою"
        try:
            invoice_url = await create_monopay_invoice(deposit_amount, description, order_id)
            buttons = [[InlineKeyboardButton("Оплатити заставу 500 грн", url=invoice_url)]]
            await query.edit_message_text(
                "Будь ласка, сплатіть заставу за посиланням нижче:", reply_markup=InlineKeyboardMarkup(buttons)
            )
            keyboard = [
                [InlineKeyboardButton("Перейти до вибору локації", callback_data="deposit_done")],
                [InlineKeyboardButton("🏠 На початок", callback_data="back:start")],
            ]
            await query.message.reply_text(
                "Після оплати натисніть кнопку нижче, щоб продовжити:", reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return DEPOSIT_PAYMENT
        except Exception as e:
            await query.edit_message_text(f"Помилка створення платежу застави: {e}")
            return ConversationHandler.END

    elif data == "start:existing_client":
        context.user_data["is_new_client"] = False
        try:
            keyboard = get_paginated_buttons(locations, 0, "location", locations_per_page, add_start_button=True)
            await query.edit_message_text(
                "Вітаємо з поверненням! Оберіть локацію:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        context.user_data["location_page"] = 0
        return CHOOSE_LOCATION

    elif data == "deposit_done":
        try:
            keyboard = get_paginated_buttons(locations, 0, "location", locations_per_page, add_start_button=True)
            await query.edit_message_text(
                "Дякуємо за оплату застави! Оберіть локацію:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        context.user_data["location_page"] = 0
        return CHOOSE_LOCATION

    elif data == "back:start":
        context.user_data.clear()
        keyboard = [
            [
                InlineKeyboardButton("Я новий клієнт", callback_data="start:new_client"),
                InlineKeyboardButton("Я вже користуюсь сервісом", callback_data="start:existing_client"),
            ]
        ]
        try:
            await query.edit_message_text(
                "Вітаємо! Оберіть, будь ласка, варіант:", reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return START_MENU

    else:
        await query.answer("Невідома дія")
        return START_MENU


async def show_locations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = get_paginated_buttons(locations, 0, "location", locations_per_page, add_start_button=True)
    try:
        await query.edit_message_text(
            "👋 *Оберіть локацію:*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
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
        try:
            await query.edit_message_text("Оберіть локацію:", reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return CHOOSE_LOCATION

    elif data == "location_prev":
        prev_page = max(current_page - 1, 0)
        context.user_data["location_page"] = prev_page
        keyboard = get_paginated_buttons(locations, prev_page, "location", locations_per_page, add_start_button=True)
        try:
            await query.edit_message_text("Оберіть локацію:", reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return CHOOSE_LOCATION

    context.user_data["location"] = data.split(":", 1)[1]
    return await show_genres(update, context)


async def show_genres(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(genre, callback_data=f"genre:{genre}")] for genre in genres]
    keyboard.append([InlineKeyboardButton("📚 Показати всі книги", callback_data="genre:all")])
    keyboard.append(
        [InlineKeyboardButton("🔙 Назад до локацій", callback_data="back:locations"),
         InlineKeyboardButton("🏠 На початок", callback_data="back:start")]
    )
    try:
        await query.edit_message_text("Оберіть жанр:", reply_markup=InlineKeyboardMarkup(keyboard))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
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
        try:
            await query.edit_message_text("Немає книг у цьому жанрі.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return ConversationHandler.END

    context.user_data["genre"] = genre
    context.user_data["books"] = all_books
    context.user_data["book_page"] = 0
    return await show_books(update, context)


async def show_books(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    books = context.user_data.get("books", [])
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
    buttons.append(
        [
            InlineKeyboardButton("🔙 До жанрів", callback_data="back:genres"),
            InlineKeyboardButton("🔙 До локацій", callback_data="back:locations"),
            InlineKeyboardButton("🏠 На початок", callback_data="back:start"),
        ]
    )
    try:
        await query.edit_message_text("Оберіть книгу:", reply_markup=InlineKeyboardMarkup(buttons))
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
    title = query.data.split(":", 1)[1]
    genre = context.user_data.get("genre")
    if genre == "all":
        books = sum(book_data.values(), [])
    else:
        books = book_data.get(genre, [])
    book = next((b for b in books if b["title"] == title), None)
    if not book:
        try:
            await query.edit_message_text("Книгу не знайдено.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return SHOW_BOOKS

    context.user_data["book"] = book
    text = (
        f"*{book['title']}*\n\n{book['desc']}\n\n"
        f"💸 *Ціна оренди:*\n"
        f"7 днів — {book.get('price_7', rental_price_map.get(7,70))} грн\n"
        f"14 днів — {book.get('price_14', rental_price_map.get(14,140))} грн"
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
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([buttons]), parse_mode="Markdown")
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
    try:
        await query.edit_message_text("Введіть ваше ім'я:")
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
    data["order_id"] = str(uuid.uuid4())
    data["chat_id"] = update.effective_chat.id

    days = int(data.get("days", 7))
    book = data.get("book", {})
    price_total = book.get(f'price_{days}', rental_price_map.get(days, 70))
    data["book"]["price"] = price_total

    logger.info("Отримане замовлення: %s", pprint.pformat(data))

    saved = await save_order_to_sheets(data)
    if not saved:
        await update.message.reply_text("Виникла проблема при збереженні замовлення. Спробуйте пізніше.")
        return ConversationHandler.END

    text = (
        f"📚 *Ваше замовлення:*\n"
        f"🏠 Локація: {data['location']}\n"
        f"📖 Книга: {data['book']['title']}\n"
        f"🗂 Жанр: {data['genre']}\n"
        f"📆 Днів: {days}\n"
        f"👤 Ім'я: {data['name']}\n"
        f"📞 Контакт: {data['contact']}\n"
        f"🆔 ID замовлення: {data['order_id']}\n\n"
        f"Сума до оплати: *{price_total} грн*"
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
        return await show_genres(update, context)
    elif data == "back:books":
        return await show_books(update, context)
    elif data == "back:locations":
        return await show_locations(update, context)
    elif data == "back:start":
        context.user_data.clear()
        keyboard = [
            [
                InlineKeyboardButton("Я новий клієнт", callback_data="start:new_client"),
                InlineKeyboardButton("Я вже користуюсь сервісом", callback_data="start:existing_client"),
            ]
        ]
        try:
            await query.edit_message_text(
                "Вітаємо! Оберіть, будь ласка, варіант:", reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return START_MENU


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


async def init_app():
    global locations, genres, book_data, rental_price_map

    load_data_from_google_sheet()

    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            START_MENU: [
                CallbackQueryHandler(start_menu_handler, pattern=r"^start:.*"),
                CallbackQueryHandler(go_back, pattern=r"^back:start$"),
            ],
            DEPOSIT_PAYMENT: [
                CallbackQueryHandler(start_menu_handler, pattern=r"^deposit_done"),
                CallbackQueryHandler(go_back, pattern=r"^back:start$"),
            ],
            CHOOSE_LOCATION: [
                CallbackQueryHandler(choose_location, pattern=r"^location.*"),
                CallbackQueryHandler(go_back, pattern=r"^back:start$"),
            ],
            CHOOSE_GENRE: [
                CallbackQueryHandler(choose_genre, pattern=r"^genre:.*"),
                CallbackQueryHandler(go_back, pattern=r"^back:(locations|start)$"),
            ],
            SHOW_BOOKS: [
                CallbackQueryHandler(book_navigation, pattern=r"^book_(next|prev)$"),
                CallbackQueryHandler(book_detail, pattern=r"^book:.*"),
                CallbackQueryHandler(go_back, pattern=r"^back:(genres|locations|start)$"),
            ],
            BOOK_DETAILS: [
                CallbackQueryHandler(choose_days, pattern=r"^days:.*"),
                CallbackQueryHandler(go_back, pattern=r"^back:(books|genres|locations|start)$"),
            ],
            CHOOSE_RENT_DAYS: [
                CallbackQueryHandler(days_chosen, pattern=r"^days:\d+$"),
                CallbackQueryHandler(go_back, pattern=r"^back:start$"),
            ],
            GET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            GET_CONTACT: [MessageHandler(filters.CONTACT | filters.TEXT, get_contact)],
            CONFIRMATION: [
                CallbackQueryHandler(confirm_payment, pattern=r"^pay_now$"),
                CallbackQueryHandler(go_back, pattern=r"^back:start$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("❌ Скасовано."))],
    )

    application.add_handler(conv_handler)

    # Додатковий обробник /start на рівні Application для перезапуску з будь-якого стану
    application.add_handler(CommandHandler("start", start))

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

