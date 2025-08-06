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
    START_MENU,
    DEPOSIT_PAYMENT,     # новий крок для оплати застави
    CHOOSE_LOCATION,
    CHOOSE_GENRE,
    SHOW_BOOKS,
    BOOK_DETAILS,
    CHOOSE_RENT_DAYS,
    GET_NAME,
    GET_CONTACT,
    CONFIRMATION,
) = range(10)

# --- Конфіг ---
locations = [f"Кав'ярня {chr(65 + i)}" for i in range(20)]
genres = ["Фантастика", "Роман", "Історія", "Детектив"]
# Залишаємо лише 2 варіанти оренди
rental_days = [7, 14]  # 7 днів та 14 днів
rental_price_map = {7: 70, 14: 140}  # Ціни за відповідний термін
books_per_page = 10
locations_per_page = 10

book_data = {
    "Фантастика": [
        {"title": f"Фантастична книга {i}", "desc": f"Це опис фантастичної книги {i}.", "price": rental_price_map[7]}
        for i in range(1, 15)
    ],
    "Роман": [
        {"title": "Анна Кареніна", "desc": "Трагічна історія кохання Анни Кареніної.", "price": rental_price_map[7]},
        {"title": "Гордість і упередження", "desc": "Класика романтичної літератури.", "price": rental_price_map[7]},
    ],
    "Історія": [{"title": "Історія України", "desc": "Огляд історії України від давнини до сьогодення.", "price": rental_price_map[7]}],
    "Детектив": [{"title": "Шерлок Холмс", "desc": "Класичні детективи про Шерлока Холмса.", "price": rental_price_map[7]}],
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


# === НОВИЙ ПОЧАТОК ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("Я новий клієнт", callback_data="start:new_client"),
            InlineKeyboardButton("Я вже користуюсь сервісом", callback_data="start:existing_client"),
        ]
    ]
    await update.message.reply_text(
        "Вітаємо! Оберіть, будь ласка, варіант:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return START_MENU


async def start_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "start:new_client":
        # Запускаємо оплату застави 500 грн через MonoPay
        deposit_amount = 500
        order_id = f"deposit_{uuid.uuid4()}"
        context.user_data["deposit_order_id"] = order_id
        context.user_data["is_new_client"] = True

        description = f"Застава за користування Тихою Поличкою"
        try:
            invoice_url = await create_monopay_invoice(deposit_amount, description, order_id)
            buttons = [[InlineKeyboardButton("Оплатити заставу 500 грн", url=invoice_url)]]
            await query.edit_message_text(
                "Будь ласка, сплатіть заставу за посиланням нижче:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            # Наступний крок — чекати підтвердження оплати (в ідеалі поки користувач перейде за посиланням)
            # Для спрощення зараз після кнопки — при повторному натисканні можна додати логіку, або чекати webhook
            # Тут ви можете порахувати, що замовлення застави оплачене після webhook.

            # Але поки — пропонуємо кнопку "Перейти до вибору локації" (у реальному випадку має бути автоматика)
            keyboard = [[InlineKeyboardButton("Перейти до вибору локації", callback_data="deposit_done")]]
            await query.message.reply_text(
                "Після оплати натисніть кнопку нижче, щоб продовжити:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return DEPOSIT_PAYMENT

        except Exception as e:
            await query.edit_message_text(f"Помилка створення платежу застави: {e}")
            return ConversationHandler.END

    elif data == "start:existing_client":
        # Прямо до вибору локацій
        context.user_data["is_new_client"] = False
        # Видаляємо це повідомлення і переходьмо до вибору локацій
        await query.edit_message_text("Вітаємо з поверненням! Оберіть локацію:")
        return await show_locations(update, context)

    elif data == "deposit_done":
        # Користувач натиснув кнопку після оплати застави
        await query.edit_message_text("Дякуємо за оплату застави! Оберіть локацію:")
        return await show_locations(update, context)

    else:
        await query.answer("Невідома дія")
        return START_MENU


# --- Далі все без змін, але з поправками для оплат днів оренди ---

async def choose_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # даємо лише 7 та 14 днів (вартість відповідна)
    buttons = [
        InlineKeyboardButton("7 днів - 70 грн", callback_data="days:7"),
        InlineKeyboardButton("14 днів - 140 грн", callback_data="days:14"),
    ]
    buttons.append(
        [
            InlineKeyboardButton("🔙 До книг", callback_data="back:books"),
            InlineKeyboardButton("🔙 До жанрів", callback_data="back:genres"),
            InlineKeyboardButton("🔙 До локацій", callback_data="back:locations"),
        ]
    )
    await query.edit_message_text("Оберіть термін оренди:", reply_markup=InlineKeyboardMarkup(buttons))
    return CHOOSE_RENT_DAYS


# Адаптуємо отримання днів оренди з callback, що тепер з двома фіксованими варіантами
async def choose_days_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await choose_days(update, context)


async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact.phone_number if update.message.contact else update.message.text.strip()
    context.user_data["contact"] = contact
    data = context.user_data
    data["order_id"] = str(uuid.uuid4())
    data["chat_id"] = update.effective_chat.id

    # Для ціни враховуємо вибраний термін + ціни фіксовані
    days = int(data.get("days", 7))
    if days not in rental_price_map:
        days = 7  # default fallback
    price_per_day = rental_price_map[days]
    # відкоригуємо ціну книги, бо ми раніше ставили базову ціну 70, а тут формуємо реальну суму
    data['book']['price'] = price_per_day

    logger.info("Отримане замовлення: %s", pprint.pformat(data))

    saved = await save_order_to_sheets(data)
    if not saved:
        await update.message.reply_text("Виникла проблема при збереженні замовлення. Спробуйте пізніше.")
        return ConversationHandler.END

    price_total = price_per_day * days
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
    button = InlineKeyboardButton("💳 Оплатити", callback_data="pay_now")
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup([[button]]), parse_mode="Markdown")
    return CONFIRMATION


# --- Інші функції без змін ---

async def confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = context.user_data
    days = int(data.get("days", 7))
    price_per_day = rental_price_map.get(days, 70)
    price_total = price_per_day * days
    description = f"Оренда книги {data['book']['title']} на {days} днів"
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
            START_MENU: [CallbackQueryHandler(start_menu_handler, pattern=r"^start:.*")],
            DEPOSIT_PAYMENT: [CallbackQueryHandler(start_menu_handler, pattern=r"^deposit_done")],
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
            CHOOSE_RENT_DAYS: [CallbackQueryHandler(choose_days_callback)],
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
