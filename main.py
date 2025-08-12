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
WEBHOOK_URL = os.getenv("WEBHOOK_URL").rstrip("/")  # Без зайвих слешів
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

        # Збереження order_id і chat_id для webhook
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
        rental_price_map = {
            7: int(df.iloc[0].get('price_7', 70)),
            14: int(df.iloc[0].get('price_14', 140))
        }
    else:
        rental_price_map = {7: 70, 14: 140}

    logger.info(f"Дані завантажено: {len(locations)} локацій, {len(genres)} жанрів.")


# --- Функції Обробники бота (start, choose_location, show_genres_for_location, тощо...)
# Через обсяг, опускаю повторення саме цих функцій,
# використовуй їх з уже наданого повного коду (попередніх відповідей),
# вони не потребують змін з приводу webhook/MonoPay.
# Якщо треба — повідом, я надішлю ще раз повний їх текст.


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

        order_id = data.get("orderId")
        payment_status = data.get("status")
        logger.info(f"MonoPay webhook received: orderId={order_id}, status={payment_status}")

        if payment_status == "PAID":
            chat_id = await get_chat_id_for_order(order_id)
            if chat_id:
                await request.app.bot_updater.bot.send_message(
                    chat_id,
                    f"✅ Оплата замовлення {order_id} успішна! Дякуємо за оренду ☕"
                )
            else:
                logger.warning(f"Chat ID for order {order_id} not found")

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
    # Заміни silent_shelf_bote на свій реальний юзернейм бота (без @)
    html_content = """
    <!DOCTYPE html>
    <html lang="uk">
    <head>
        <meta charset="UTF-8" />
        <title>Оплата успішна</title>
    </head>
    <body style="font-family: Arial, sans-serif; text-align: center; margin-top: 50px;">
        <h1>Оплата успішна! Дякуємо за оренду.</h1>
        <p>
            <a href="https://t.me/silent_shelf_bote" style="font-size: 18px; color: #0088cc; text-decoration: none;">
                Повернутися в бот
            </a>
        </p>
    </body>
    </html>
    """
    return web.Response(text=html_content, content_type='text/html')


async def init_app():
    load_data_from_google_sheet()

    application = Application.builder().token(BOT_TOKEN).build()

    # Тут потрібно додати своїх обробників (CommandHandler, CallbackQueryHandler і т.п.)
    # Використовуй свої з наданого коду, наприклад:

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
