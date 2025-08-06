import os
import json
import logging
import pprint
import hmac
import hashlib
import uuid

from dotenv import load_dotenv
from aiohttp import web, ClientSession

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
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

# --- Змінні середовища ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONOPAY_TOKEN = os.getenv("MONOPAY_TOKEN")
MONOPAY_WEBHOOK_SECRET = os.getenv("MONOPAY_WEBHOOK_SECRET", None)
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # https://yourdomain.com без кінцевого слеша
PORT = int(os.getenv("PORT", 8443))

# Conversation handler states
(
    CHOOSE_LOCATION,
    CHOOSE_GENRE,
    SHOW_BOOKS,
    BOOK_DETAILS,
    CHOOSE_RENT_DAYS,
    GET_NAME,
    GET_CONTACT,
    CONFIRMATION,
) = range(8)

# --- Конфіг ---
locations = [f"Кав'ярня {chr(65 + i)}" for i in range(20)]
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
        {"title": "Гордість і упередження", "desc": "Класика романтичної літератури.", "price": 35},
    ],
    "Історія": [{"title": "Історія України", "desc": "Огляд історії України від давнини до сьогодення.", "price": 50}],
    "Детектив": [{"title": "Шерлок Холмс", "desc": "Класичні детективи про Шерлока Холмса.", "price": 45}],
}

# --- Google Sheets ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds_dict = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))
credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.Client(auth=credentials)
gc.session = AuthorizedSession(credentials)
sh = gc.open("RentalBookBot")
worksheet = sh.sheet1


def get_paginated_buttons(items, page, prefix, page_size):
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
    return buttons


async def create_monopay_invoice(amount: int, description: str, order_id: str) -> str:
    url = "https://api.monobank.ua/api/merchant/invoice/create"
    headers = {
        "X-Token": MONOPAY_TOKEN,
        "Content-Type": "application/json",
    }
    data = {
        "amount": amount * 100,  # сума в копійках
        "currency": 980,
        "description": description,
        "orderId": order_id,
        "redirectUrl": f"{WEBHOOK_URL}/success",
        "webHookUrl": f"{WEBHOOK_URL}/monopay_callback",
    }
    async with ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as response:
            resp_json = await response.json()
            if response.status == 200 and ("pageUrl" in resp_json or "invoiceUrl" in resp_json):
                # MonoPay іноді повертає pageUrl замість invoiceUrl
                return resp_json.get("pageUrl") or resp_json.get("invoiceUrl")
            else:
                logger.error(f"MonoPay invoice creation error: {resp_json}")
                raise Exception(f"Помилка створення інвойсу MonoPay: {resp_json}")


async def save_order_to_sheets(data: dict) -> bool:
    try:
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
        logger.error(f"Помилка запису в Google Sheets: {e}")
        return False


async def get_chat_id_for_order(order_id: str) -> int | None:
    try:
        records = worksheet.get_all_records()
        for row in records:
            if str(row.get("order_id", "")) == str(order_id):
                chat_id = row.get("chat_id")
                if chat_id:
                    return int(chat_id)
    except Exception as e:
        logger.error(f"Error getting chat_id for order: {e}")
    return None


# --- Telegram handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = get_paginated_buttons(locations, 0, "location", locations_per_page)
    text = (
        "👋 *Вас вітає Тиха Поличка!*\n"
        "Сучасний і зручний спосіб оренди книжок у затишних місцях.\n\n"
        "Оберіть локацію:"
    )
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    context.user_data["location_page"] = 0
    return CHOOSE_LOCATION


async def show_locations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        keyboard = get_paginated_buttons(locations, 0, "location", locations_per_page)
        await query.edit_message_text(
            "👋 *Вас вітає Тиха Поличка!*\n"
            "Сучасний і зручний спосіб оренди книжок у затишних місцях.\n\n"
            "Оберіть локацію:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        context.user_data["location_page"] = 0
        return CHOOSE_LOCATION
    else:
        return await start(update, context)


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
        context.user_data["location_page"] = max(page - 1, 0)
        keyboard = get_paginated_buttons(locations, context.user_data["location_page"], "location", locations_per_page)
        await query.edit_message_text("Оберіть локацію:", reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSE_LOCATION

    context.user_data["location"] = data.split(":", 1)[1]
    return await show_genres(update, context)


async def show_genres(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        message_func = query.edit_message_text
    else:
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
        ]
    )

    await query.edit_message_text("Оберіть книгу:", reply_markup=InlineKeyboardMarkup(buttons))
    return SHOW_BOOKS


async def book_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "book_next":
        context.user_data["book_page"] = context.user_data.get("book_page", 0) + 1
    elif query.data == "book_prev":
        context.user_data["book_page"] = max(context.user_data.get("book_page", 0) - 1, 0)
    return await show_books(update, context)


async def book_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    title = query.data.split(":", 1)[1]
    genre = context.user_data.get("genre")
    books = book_data.get(genre, []) if genre != "all" else sum(book_data.values(), [])
    book = next((b for b in books if b["title"] == title), None)

    if not book:
        await query.edit_message_text("Книгу не знайдено.")
        return SHOW_BOOKS

    context.user_data["book"] = book
    text = f"*{book['title']}*\n\n{book['desc']}\n\n💸 *Ціна оренди за день*: {book['price']} грн"
    buttons = [[InlineKeyboardButton(f"{d} днів", callback_data=f"days:{d}")] for d in rental_days]
    buttons.append(
        [
            InlineKeyboardButton("🔙 До книг", callback_data="back:books"),
            InlineKeyboardButton("🔙 До жанрів", callback_data="back:genres"),
            InlineKeyboardButton("🔙 До локацій", callback_data="back:locations"),
        ]
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return BOOK_DETAILS


async def choose_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["days"] = query.data.split(":", 1)[1]
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

    data = context.user_data

    data["order_id"] = str(uuid.uuid4())
    data["chat_id"] = update.effective_chat.id

    logger.info("Отримане замовлення: %s", pprint.pformat(data))

    saved = await save_order_to_sheets(data)
    if not saved:
        await update.message.reply_text("Виникла проблема при збереженні замовлення. Спробуйте пізніше.")
        return ConversationHandler.END

    price_total = data["book"]["price"] * int(data["days"])
    text = (
        f"📚 *Ваше замовлення:*\n"
        f"🏠 Локація: {data['location']}\n"
        f"📖 Книга: {data['book']['title']}\n"
        f"🗂 Жанр: {data['genre']}\n"
        f"📆 Днів: {data['days']}\n"
        f"👤 Ім'я: {data['name']}\n"
        f"📞 Контакт: {data['contact']}\n"
        f"🆔 ID замовлення: {data['order_id']}\n\n"
        f"Сума до оплати: *{price_total} грн*"
    )
    button = InlineKeyboardButton("💳 Оплатити", callback_data="pay_now")
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup([[button]]), parse_mode="Markdown")
    return CONFIRMATION


async def confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = context.user_data
    price_total = data["book"]["price"] * int(data["days"])
    description = f"Оренда книги {data['book']['title']} на {data['days']} днів"
    order_id = data["order_id"]

    try:
        invoice_url = await create_monopay_invoice(price_total, description, order_id)
        buttons = [[InlineKeyboardButton("Оплатити MonoPay", url=invoice_url)]]
        await query.edit_message_text(
            "Будь ласка, оплатіть за посиланням нижче:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Помилка створення інвойсу MonoPay: {e}")
        await query.edit_message_text(f"Сталася помилка при створенні платежу: {e}")
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


# Обробка webhook від MonoPay
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
            await request.app.bot_updater.bot.send_message(
                chat_id, f"✅ Оплата замовлення {order_id} успішна! Дякуємо за оренду ☕"
            )

        return web.Response(text="OK")

    except Exception as e:
        logger.exception("Error in MonoPay webhook:")
        return web.Response(text=f"Error: {e}", status=500)


# Telegram webhook handler aiohttp
async def telegram_webhook_handler(request):
    app = request.app
    bot_app = app.bot_updater
    body = await request.text()
    update = Update.de_json(json.loads(body), bot_app.bot)
    await bot_app.process_update(update)
    return web.Response(text="OK", status=200)


# Ініціалізація і запуск aiohttp та Telegram Application
async def init_app():
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_LOCATION: [CallbackQueryHandler(choose_location, pattern=r"^location.*")],
            CHOOSE_GENRE: [
                CallbackQueryHandler(choose_genre, pattern=r"^genre:.*"),
                CallbackQueryHandler(go_back, pattern=r"^back:locations$"),
            ],
            SHOW_BOOKS: [
                CallbackQueryHandler(book_navigation, pattern=r"^book_(next|prev)$"),
                CallbackQueryHandler(book_detail, pattern=r"^book:.*"),
                CallbackQueryHandler(go_back, pattern=r"^back:(genres|locations)$"),
            ],
            BOOK_DETAILS: [
                CallbackQueryHandler(choose_days, pattern=r"^days:.*"),
                CallbackQueryHandler(go_back, pattern=r"^back:(books|genres|locations)$"),
            ],
            CHOOSE_RENT_DAYS: [CallbackQueryHandler(choose_days)],
            GET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            GET_CONTACT: [MessageHandler(filters.CONTACT | filters.TEXT, get_contact)],
            CONFIRMATION: [CallbackQueryHandler(confirm_payment, pattern=r"^pay_now$")],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("❌ Скасовано."))],
    )

    application.add_handler(conv_handler)

    # Обов'язкова ініціалізація та старт!
    await application.initialize()
    await application.start()

    app = web.Application()
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
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app, application = loop.run_until_complete(init_app())
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        loop.run_until_complete(application.stop())
        loop.run_until_complete(application.shutdown())
