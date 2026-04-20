import os
import logging
import httpx
import yfinance as yf
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler

# --- Config ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

DB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}
DB_URL = f"{SUPABASE_URL}/rest/v1/stocks"

logging.basicConfig(level=logging.INFO)

# Conversation states
WAITING_FOR_STOCK_ADD = 1
WAITING_FOR_STOCK_CHECK = 2
WAITING_FOR_STOCK_REMOVE = 3

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
    keyboard = [
        [
            InlineKeyboardButton("➕ Add Stock", callback_data="menu_add"),
            InlineKeyboardButton("📊 Check Stock", callback_data="menu_check"),
        ],
        [
            InlineKeyboardButton("📈 Portfolio", callback_data="menu_portfolio"),
            InlineKeyboardButton("🗑️ Remove Stock", callback_data="menu_remove"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

# --- /start & /help ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to your Stock Tracker Bot!*\n\n"
        "What would you like to do?",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Stock Tracker Bot*\n\n"
        "What would you like to do?",
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
            "➕ *Add a Stock*\n\nType the NSE symbol of the stock you want to track:\n\n"
            "Example: `RELIANCE` or `INFY` or `TCS`",
            parse_mode="Markdown"
        )
        return WAITING_FOR_STOCK_ADD

    elif data == "menu_check":
        await query.message.reply_text(
            "📊 *Check a Stock*\n\nType the NSE symbol to check its performance:\n\n"
            "Example: `RELIANCE` or `INFY` or `TCS`",
            parse_mode="Markdown"
        )
        return WAITING_FOR_STOCK_CHECK

    elif data == "menu_remove":
        user_id = query.from_user.id
        stocks = db_select_all(user_id)
        if not stocks:
            await query.message.reply_text("📭 You have no stocks tracked yet.")
            return ConversationHandler.END

        # Show buttons for each tracked stock
        keyboard = [[InlineKeyboardButton(f"🗑️ {s['stock_name']}", callback_data=f"remove_{s['stock_name']}")] for s in stocks]
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        await query.message.reply_text(
            "🗑️ *Remove a Stock*\n\nSelect which stock to remove:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    elif data == "menu_portfolio":
        user_id = query.from_user.id
        await show_portfolio(query.message, user_id)
        return ConversationHandler.END

    elif data.startswith("remove_"):
        stock_name = data.replace("remove_", "")
        user_id = query.from_user.id
        db_delete({"user_id": user_id, "stock_name": stock_name})
        await query.message.reply_text(
            f"🗑️ Removed *{stock_name}* from your tracker.\n\nWhat's next?",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    elif data == "cancel":
        await query.message.reply_text(
            "❌ Cancelled. What would you like to do?",
            reply_markup=main_menu_keyboard()
        )
        return ConversationHandler.END

    elif data == "back_menu":
        await query.message.reply_text(
            "What would you like to do?",
            reply_markup=main_menu_keyboard()
        )
        return ConversationHandler.END

# --- Handle stock name input for ADD ---
async def handle_add_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock_name = update.message.text.strip().upper()
    user_id = update.effective_user.id

    msg = await update.message.reply_text(f"⏳ Fetching live price for *{stock_name}*...", parse_mode="Markdown")

    price = get_live_price(stock_name)
    if price is None:
        await update.message.reply_text(
            f"❌ Could not fetch price for *{stock_name}*.\n"
            f"Make sure it's a valid NSE symbol.\n\nTry again or go back:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_menu")]]),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    existing = db_select({"user_id": user_id, "stock_name": stock_name})
    if existing:
        await update.message.reply_text(
            f"⚠️ Already tracking *{stock_name}*\n"
            f"📌 Entry price: ₹{existing[0]['entry_price']}\n\n"
            f"Use Remove first to reset it.",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    db_insert({
        "user_id": user_id,
        "stock_name": stock_name,
        "exchange": "NSE",
        "entry_price": price,
        "entry_date": str(date.today())
    })

    await update.message.reply_text(
        f"✅ Now tracking *{stock_name}*\n"
        f"📌 Entry Price: ₹{price}\n"
        f"📅 Date: {date.today().strftime('%d %b %Y')}\n\n"
        f"What's next?",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# --- Handle stock name input for CHECK ---
async def handle_check_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock_name = update.message.text.strip().upper()
    user_id = update.effective_user.id

    result = db_select({"user_id": user_id, "stock_name": stock_name})
    if not result:
        await update.message.reply_text(
            f"❌ You're not tracking *{stock_name}*.\n"
            f"Add it first using the menu below.",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    await update.message.reply_text(f"⏳ Fetching live price for *{stock_name}*...", parse_mode="Markdown")

    row = result[0]
    entry_price = float(row["entry_price"])
    entry_date = date.fromisoformat(row["entry_date"])
    days_held = (date.today() - entry_date).days

    current_price = get_live_price(stock_name)
    if current_price is None:
        await update.message.reply_text(
            f"❌ Could not fetch current price for *{stock_name}*.",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    change_pct = ((current_price - entry_price) / entry_price) * 100
    arrow = "🟢" if change_pct >= 0 else "🔴"
    sign = "+" if change_pct >= 0 else ""

    await update.message.reply_text(
        f"📊 *{stock_name}*\n\n"
        f"📌 Entry Price: ₹{entry_price}\n"
        f"💹 Current Price: ₹{current_price}\n"
        f"{arrow} Change: {sign}{change_pct:.2f}%\n"
        f"📅 Added: {entry_date.strftime('%d %b %Y')} ({days_held} days ago)\n\n"
        f"What's next?",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# --- Show Portfolio ---
async def show_portfolio(message, user_id):
    result = db_select_all(user_id)
    if not result:
        await message.reply_text(
            "📭 No stocks tracked yet.",
            reply_markup=main_menu_keyboard()
        )
        return

    await message.reply_text("⏳ Fetching live prices for your portfolio...")
    lines = ["📈 *Your Portfolio*\n"]

    for row in result:
        stock_name = row["stock_name"]
        entry_price = float(row["entry_price"])
        entry_date = date.fromisoformat(row["entry_date"])
        days_held = (date.today() - entry_date).days
        current_price = get_live_price(stock_name)

        if current_price is None:
            lines.append(f"• {stock_name} — ❌ Price unavailable\n")
            continue

        change_pct = ((current_price - entry_price) / entry_price) * 100
        arrow = "🟢" if change_pct >= 0 else "🔴"
        sign = "+" if change_pct >= 0 else ""
        lines.append(
            f"{arrow} *{stock_name}*\n"
            f"   Entry: ₹{entry_price} → Now: ₹{current_price}\n"
            f"   Change: {sign}{change_pct:.2f}% | {days_held}d held\n"
        )

    await message.reply_text(
        "\n".join(lines),
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )

# --- Main ---
if __name__ == "__main__":
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
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("help", help_command),
        ],
        per_message=False,
    )

    app.add_handler(conv_handler)
    print("🤖 Bot is running...")
    app.run_polling()