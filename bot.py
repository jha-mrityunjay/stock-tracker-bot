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

WAITING_ADD = 1
WAITING_CHECK = 2
WAITING_REMOVE = 3

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

# --- /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to Stock Tracker Bot!*\n\n"
        "Type `/` to see all available commands.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# --- /add ---
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # If symbol given directly e.g. /add INFY
    if context.args:
        update.message.text = context.args[0]
        return await handle_add(update, context)
    await update.message.reply_text(
        "➕ *Add a Stock*\n\nType the NSE symbol to track:\n\nExample: `RELIANCE`",
        parse_mode="Markdown"
    )
    return WAITING_ADD

# --- /check ---
async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        update.message.text = context.args[0]
        return await handle_check(update, context)
    await update.message.reply_text(
        "📊 *Check a Stock*\n\nType the NSE symbol to check:\n\nExample: `TCS`",
        parse_mode="Markdown"
    )
    return WAITING_CHECK

# --- /portfolio ---
async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    result = db_select_all(user_id)

    if not result:
        await update.message.reply_text("📭 No stocks tracked yet.\n\nUse /add to start tracking!")
        return ConversationHandler.END

    await update.message.reply_text("⏳ Fetching live prices...")
    lines = ["📈 *Your Portfolio*\n"]
    for row in result:
        stock_name = row["stock_name"]
        entry_price = float(row["entry_price"])
        entry_date = date.fromisoformat(row["entry_date"])
        days_held = (date.today() - entry_date).days
        current_price = get_live_price(stock_name)
        if current_price is None:
            lines.append(f"• *{stock_name}* — ❌ Price unavailable\n")
            continue
        change_pct = ((current_price - entry_price) / entry_price) * 100
        arrow = "🟢" if change_pct >= 0 else "🔴"
        sign = "+" if change_pct >= 0 else ""
        lines.append(
            f"{arrow} *{stock_name}*\n"
            f"   Entry ₹{entry_price} → Now ₹{current_price}\n"
            f"   {sign}{change_pct:.2f}% | {days_held} days\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return ConversationHandler.END

# --- /remove ---
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    stocks = db_select_all(user_id)

    if not stocks:
        await update.message.reply_text("📭 No stocks tracked yet.")
        return ConversationHandler.END

    # Show inline buttons for each stock
    keyboard = [[InlineKeyboardButton(f"🗑️ {s['stock_name']}", callback_data=f"remove_{s['stock_name']}")] for s in stocks]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    await update.message.reply_text(
        "🗑️ *Which stock to remove?*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# --- /help ---
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Stock Tracker Bot Commands*\n\n"
        "/add — Add a stock to track\n"
        "/check — Check stock % change\n"
        "/portfolio — View all tracked stocks\n"
        "/remove — Remove a tracked stock\n"
        "/help — Show this message\n\n"
        "💡 You can also type `/add INFY` directly!",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# --- Handle text input for ADD ---
async def handle_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock_name = update.message.text.strip().upper()
    user_id = update.effective_user.id

    await update.message.reply_text(f"⏳ Fetching *{stock_name}* from NSE...", parse_mode="Markdown")

    price = get_live_price(stock_name)
    if price is None:
        await update.message.reply_text(
            f"❌ *{stock_name}* not found.\nCheck the NSE symbol and try again with /add",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    existing = db_select({"user_id": user_id, "stock_name": stock_name})
    if existing:
        await update.message.reply_text(
            f"⚠️ Already tracking *{stock_name}* at ₹{existing[0]['entry_price']}\n"
            f"Use /remove first to reset it.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    db_insert({"user_id": user_id, "stock_name": stock_name, "exchange": "NSE",
               "entry_price": price, "entry_date": str(date.today())})

    await update.message.reply_text(
        f"✅ *{stock_name}* added!\n"
        f"📌 Entry Price: ₹{price}\n"
        f"📅 {date.today().strftime('%d %b %Y')}\n\n"
        f"Use /portfolio to see all your stocks.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# --- Handle text input for CHECK ---
async def handle_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock_name = update.message.text.strip().upper()
    user_id = update.effective_user.id

    result = db_select({"user_id": user_id, "stock_name": stock_name})
    if not result:
        await update.message.reply_text(
            f"❌ Not tracking *{stock_name}*.\nUse /add {stock_name} to start tracking it.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    await update.message.reply_text(f"⏳ Fetching *{stock_name}* from NSE...", parse_mode="Markdown")

    row = result[0]
    entry_price = float(row["entry_price"])
    entry_date = date.fromisoformat(row["entry_date"])
    days_held = (date.today() - entry_date).days
    current_price = get_live_price(stock_name)

    if current_price is None:
        await update.message.reply_text(f"❌ Could not fetch price for *{stock_name}* right now.", parse_mode="Markdown")
        return ConversationHandler.END

    change_pct = ((current_price - entry_price) / entry_price) * 100
    arrow = "🟢" if change_pct >= 0 else "🔴"
    sign = "+" if change_pct >= 0 else ""

    await update.message.reply_text(
        f"📊 *{stock_name}*\n\n"
        f"📌 Entry: ₹{entry_price}\n"
        f"💹 Current: ₹{current_price}\n"
        f"{arrow} Change: {sign}{change_pct:.2f}%\n"
        f"📅 Added {entry_date.strftime('%d %b %Y')} ({days_held} days ago)",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# --- Remove callback ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("remove_"):
        stock_name = query.data.replace("remove_", "")
        db_delete({"user_id": query.from_user.id, "stock_name": stock_name})
        await query.message.edit_text(f"🗑️ *{stock_name}* removed successfully!", parse_mode="Markdown")

    elif query.data == "cancel":
        await query.message.edit_text("❌ Cancelled.")

# --- Web server for Render ---
async def health(request):
    return web.Response(text="OK")

async def run_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# --- Main ---
async def main():
    await run_web_server()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("help", cmd_help),
            CommandHandler("add", cmd_add),
            CommandHandler("check", cmd_check),
            CommandHandler("portfolio", cmd_portfolio),
            CommandHandler("remove", cmd_remove),
            CallbackQueryHandler(button_handler),
        ],
        states={
            WAITING_ADD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add)],
            WAITING_CHECK: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_check)],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("add", cmd_add),
            CommandHandler("check", cmd_check),
            CommandHandler("portfolio", cmd_portfolio),
            CommandHandler("remove", cmd_remove),
        ],
        per_message=False,
    )

    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(button_handler))

    print("🤖 Bot is running...")
    await app.initialize()
    await app.start()
    # Clear any existing webhook or running instance
    await app.bot.delete_webhook(drop_pending_updates=True)
    await app.updater.start_polling(drop_pending_updates=True)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())