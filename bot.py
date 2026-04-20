import os
import logging
import httpx
import yfinance as yf
from datetime import date
import asyncio
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler

# --- Config ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
PORT = int(os.environ.get("PORT", 8080))

DB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}
DB_URL = f"{SUPABASE_URL}/rest/v1/stocks"

logging.basicConfig(level=logging.INFO)

WAITING_FOR_STOCK_ADD = 1
WAITING_FOR_STOCK_CHECK = 2

# --- DB Helpers ---
def db_select(filters: dict):
    params = {"select": "*"}
    for k, v in filters.items():
        params[k] = f"eq.{v}"
    r = httpx.get(DB_URL, headers=DB_HEADERS, params=params)
    result = r.json()
    return result if isinstance(result, list) else []

def db_select_all(user_id):
    params = {"select": "*", "user_id": f"eq.{user_id}"}
    r = httpx.get(DB_URL, headers=DB_HEADERS, params=params)
    result = r.json()
    return result if isinstance(result, list) else []

def db_insert(data: dict):
    httpx.post(DB_URL, headers=DB_HEADERS, json=data)

def db_delete(filters: dict):
    params = {}
    for k, v in filters.items():
        params[k] = f"eq.{v}"
    httpx.delete(DB_URL, headers=DB_HEADERS, params=params)

# --- Price Fetch ---
def get_live_price(stock_name: str):
    try:
        ticker = yf.Ticker(f"{stock_name.upper()}.NS")
        price = ticker.fast_info.get("last_price") or ticker.fast_info.get("previous_close")
        if price and float(price) > 0:
            return round(float(price), 2)
        data = ticker.history(period="5d")
        if not data.empty:
            return round(float(data["Close"].iloc[-1]), 2)
        return None
    except Exception as e:
        logging.error(f"Price fetch error for {stock_name}: {e}")
        return None

# --- Main Menu ---
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Stock", callback_data="menu_add"),
         InlineKeyboardButton("📊 Check Stock", callback_data="menu_check")],
        [InlineKeyboardButton("📈 Portfolio", callback_data="menu_portfolio"),
         InlineKeyboardButton("🗑️ Remove Stock", callback_data="menu_remove")],
    ])

# --- /start & /help ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to your Stock Tracker Bot!*\n\nWhat would you like to do?",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Stock Tracker Bot*\n\nWhat would you like to do?",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# --- Button Callbacks ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_add":
        await query.message.reply_text(
            "➕ *Add a Stock*\n\nType the NSE symbol:\nExample: `RELIANCE` or `INFY` or `TCS`",
            parse_mode="Markdown"
        )
        return WAITING_FOR_STOCK_ADD

    elif data == "menu_check":
        await query.message.reply_text(
            "📊 *Check a Stock*\n\nType the NSE symbol:\nExample: `RELIANCE` or `INFY` or `TCS`",
            parse_mode="Markdown"
        )
        return WAITING_FOR_STOCK_CHECK

    elif data == "menu_remove":
        user_id = query.from_user.id
        stocks = db_select_all(user_id)
        if not stocks:
            await query.message.reply_text("📭 No stocks tracked yet.", reply_markup=main_menu_keyboard())
            return ConversationHandler.END
        keyboard = [[InlineKeyboardButton(f"🗑️ {s['stock_name']}", callback_data=f"remove_{s['stock_name']}")] for s in stocks]
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        await query.message.reply_text(
            "🗑️ *Select stock to remove:*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    elif data == "menu_portfolio":
        await show_portfolio(query.message, query.from_user.id)
        return ConversationHandler.END

    elif data.startswith("remove_"):
        stock_name = data.replace("remove_", "")
        db_delete({"user_id": query.from_user.id, "stock_name": stock_name})
        await query.message.reply_text(
            f"🗑️ Removed *{stock_name}*\n\nWhat's next?",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    elif data in ("cancel", "back_menu"):
        await query.message.reply_text("What would you like to do?", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

# --- Handle ADD input ---
async def handle_add_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock_name = update.message.text.strip().upper()
    user_id = update.effective_user.id

    await update.message.reply_text(f"⏳ Fetching live price for *{stock_name}*...", parse_mode="Markdown")

    price = get_live_price(stock_name)
    if price is None:
        await update.message.reply_text(
            f"❌ Could not fetch price for *{stock_name}*.\nMake sure it's a valid NSE symbol.",
            reply_markup=main_menu_keyboard(), parse_mode="Markdown"
        )
        return ConversationHandler.END

    existing = db_select({"user_id": user_id, "stock_name": stock_name})
    if existing:
        await update.message.reply_text(
            f"⚠️ Already tracking *{stock_name}*\n📌 Entry: ₹{existing[0]['entry_price']}\n\nRemove it first to reset.",
            reply_markup=main_menu_keyboard(), parse_mode="Markdown"
        )
        return ConversationHandler.END

    db_insert({"user_id": user_id, "stock_name": stock_name, "exchange": "NSE", "entry_price": price, "entry_date": str(date.today())})
    await update.message.reply_text(
        f"✅ Now tracking *{stock_name}*\n📌 Entry Price: ₹{price}\n📅 {date.today().strftime('%d %b %Y')}\n\nWhat's next?",
        reply_markup=main_menu_keyboard(), parse_mode="Markdown"
    )
    return ConversationHandler.END

# --- Handle CHECK input ---
async def handle_check_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock_name = update.message.text.strip().upper()
    user_id = update.effective_user.id

    result = db_select({"user_id": user_id, "stock_name": stock_name})
    if not result:
        await update.message.reply_text(
            f"❌ Not tracking *{stock_name}*. Add it first.",
            reply_markup=main_menu_keyboard(), parse_mode="Markdown"
        )
        return ConversationHandler.END

    await update.message.reply_text(f"⏳ Fetching live price for *{stock_name}*...", parse_mode="Markdown")

    row = result[0]
    entry_price = float(row["entry_price"])
    entry_date = date.fromisoformat(row["entry_date"])
    days_held = (date.today() - entry_date).days
    current_price = get_live_price(stock_name)

    if current_price is None:
        await update.message.reply_text(f"❌ Could not fetch price for *{stock_name}*.", reply_markup=main_menu_keyboard(), parse_mode="Markdown")
        return ConversationHandler.END

    change_pct = ((current_price - entry_price) / entry_price) * 100
    arrow = "🟢" if change_pct >= 0 else "🔴"
    sign = "+" if change_pct >= 0 else ""

    await update.message.reply_text(
        f"📊 *{stock_name}*\n\n📌 Entry: ₹{entry_price}\n💹 Current: ₹{current_price}\n{arrow} Change: {sign}{change_pct:.2f}%\n📅 {entry_date.strftime('%d %b %Y')} ({days_held}d ago)\n\nWhat's next?",
        reply_markup=main_menu_keyboard(), parse_mode="Markdown"
    )
    return ConversationHandler.END

# --- Portfolio ---
async def show_portfolio(message, user_id):
    result = db_select_all(user_id)
    if not result:
        await message.reply_text("📭 No stocks tracked yet.", reply_markup=main_menu_keyboard())
        return

    await message.reply_text("⏳ Fetching live prices...")
    lines = ["📈 *Your Portfolio*\n"]
    for row in result:
        stock_name = row["stock_name"]
        entry_price = float(row["entry_price"])
        entry_date = date.fromisoformat(row["entry_date"])
        days_held = (date.today() - entry_date).days
        current_price = get_live_price(stock_name)
        if current_price is None:
            lines.append(f"• {stock_name} — ❌ Unavailable\n")
            continue
        change_pct = ((current_price - entry_price) / entry_price) * 100
        arrow = "🟢" if change_pct >= 0 else "🔴"
        sign = "+" if change_pct >= 0 else ""
        lines.append(f"{arrow} *{stock_name}*\n   ₹{entry_price} → ₹{current_price} | {sign}{change_pct:.2f}% | {days_held}d\n")

    await message.reply_text("\n".join(lines), reply_markup=main_menu_keyboard(), parse_mode="Markdown")

# --- Web server health check (keeps Render happy) ---
async def health(request):
    return web.Response(text="OK")

async def run_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Web server running on port {PORT}")

# --- Main ---
async def main():
    # Start web server first so Render doesn't time out
    await run_web_server()

    # Build telegram bot
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("help", help_command),
            CallbackQueryHandler(button_handler),
        ],
        states={
            WAITING_FOR_STOCK_ADD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_stock),
                CallbackQueryHandler(button_handler),
            ],
            WAITING_FOR_STOCK_CHECK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_check_stock),
                CallbackQueryHandler(button_handler),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        per_message=False,
    )

    app.add_handler(conv_handler)
    print("🤖 Bot is running...")

    # Run bot polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # Keep running forever
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())