import os
import logging
import httpx
import yfinance as yf
from datetime import date
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

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
        # Try fast_info first (most reliable)
        price = ticker.fast_info.get("last_price") or ticker.fast_info.get("previous_close")
        if price and float(price) > 0:
            return round(float(price), 2)
        # Fallback to history
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
        "👋 Welcome to your Stock Tracker Bot!\n\n"
        "Commands:\n"
        "/add INFY — Start tracking a stock\n"
        "/check INFY — Check % change & days held\n"
        "/portfolio — See all tracked stocks\n"
        "/remove INFY — Stop tracking a stock\n"
        "/help — Show this menu"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# --- /add ---
async def add_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("❌ Usage: /add INFY")
        return

    stock_name = context.args[0].upper()
    await update.message.reply_text(f"⏳ Fetching live price for {stock_name}...")

    price = get_live_price(stock_name)
    if price is None:
        await update.message.reply_text(
            f"❌ Could not fetch price for {stock_name}.\n"
            f"Make sure it's a valid NSE symbol (e.g. RELIANCE, INFY, TCS)"
        )
        return

    existing = db_select({"user_id": user_id, "stock_name": stock_name})
    if existing:
        await update.message.reply_text(
            f"⚠️ Already tracking {stock_name}.\n"
            f"Entry price: ₹{existing[0]['entry_price']}\n"
            f"Use /remove {stock_name} first to reset."
        )
        return

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
        f"📅 Date: {date.today().strftime('%d %b %Y')}",
        parse_mode="Markdown"
    )

# --- /check ---
async def check_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("❌ Usage: /check INFY")
        return

    stock_name = context.args[0].upper()
    result = db_select({"user_id": user_id, "stock_name": stock_name})

    if not result:
        await update.message.reply_text(f"❌ Not tracking {stock_name}. Use /add {stock_name} first.")
        return

    row = result[0]
    entry_price = float(row["entry_price"])
    entry_date = date.fromisoformat(row["entry_date"])
    days_held = (date.today() - entry_date).days

    current_price = get_live_price(stock_name)
    if current_price is None:
        await update.message.reply_text(f"❌ Could not fetch current price for {stock_name}.")
        return

    change_pct = ((current_price - entry_price) / entry_price) * 100
    arrow = "🟢" if change_pct >= 0 else "🔴"
    sign = "+" if change_pct >= 0 else ""

    await update.message.reply_text(
        f"📊 *{stock_name}*\n\n"
        f"📌 Entry Price: ₹{entry_price}\n"
        f"💹 Current Price: ₹{current_price}\n"
        f"{arrow} Change: {sign}{change_pct:.2f}%\n"
        f"📅 Added: {entry_date.strftime('%d %b %Y')} ({days_held} days ago)",
        parse_mode="Markdown"
    )

# --- /portfolio ---
async def portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    result = db_select_all(user_id)

    if not result:
        await update.message.reply_text("📭 No stocks tracked yet. Use /add INFY to start.")
        return

    await update.message.reply_text("⏳ Fetching live prices...")
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

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# --- /remove ---
async def remove_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("❌ Usage: /remove INFY")
        return

    stock_name = context.args[0].upper()
    result = db_select({"user_id": user_id, "stock_name": stock_name})

    if not result:
        await update.message.reply_text(f"❌ You're not tracking {stock_name}.")
        return

    db_delete({"user_id": user_id, "stock_name": stock_name})
    await update.message.reply_text(f"🗑️ Removed *{stock_name}* from your tracker.", parse_mode="Markdown")

# --- Main ---
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("add", add_stock))
    app.add_handler(CommandHandler("check", check_stock))
    app.add_handler(CommandHandler("portfolio", portfolio))
    app.add_handler(CommandHandler("remove", remove_stock))
    print("🤖 Bot is running...")
    app.run_polling()