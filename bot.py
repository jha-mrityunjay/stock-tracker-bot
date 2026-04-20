import os
import logging
from datetime import date
import yfinance as yf
from supabase import create_client, Client
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- Config from environment variables ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# --- Init clients ---
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(level=logging.INFO)

# --- Helper: Fetch live price from Yahoo Finance ---
def get_live_price(stock_name: str):
    ticker = yf.Ticker(f"{stock_name.upper()}.NS")
    data = ticker.history(period="1d")
    if data.empty:
        return None
    return round(data["Close"].iloc[-1], 2)

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

# --- /help ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# --- /add STOCKNAME ---
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
            f"❌ Could not fetch price for {stock_name}. Make sure it's a valid NSE symbol."
        )
        return

    # Check if already tracking
    existing = supabase.table("stocks").select("*")\
        .eq("user_id", user_id).eq("stock_name", stock_name).execute()

    if existing.data:
        await update.message.reply_text(
            f"⚠️ You're already tracking {stock_name}.\n"
            f"Entry price: ₹{existing.data[0]['entry_price']}\n"
            f"Use /remove {stock_name} first if you want to reset it."
        )
        return

    # Save to Supabase
    supabase.table("stocks").insert({
        "user_id": user_id,
        "stock_name": stock_name,
        "exchange": "NSE",
        "entry_price": price,
        "entry_date": str(date.today())
    }).execute()

    await update.message.reply_text(
        f"✅ Now tracking *{stock_name}*\n"
        f"📌 Entry Price: ₹{price}\n"
        f"📅 Date: {date.today().strftime('%d %b %Y')}",
        parse_mode="Markdown"
    )

# --- /check STOCKNAME ---
async def check_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text("❌ Usage: /check INFY")
        return

    stock_name = context.args[0].upper()

    result = supabase.table("stocks").select("*")\
        .eq("user_id", user_id).eq("stock_name", stock_name).execute()

    if not result.data:
        await update.message.reply_text(
            f"❌ You're not tracking {stock_name}. Use /add {stock_name} first."
        )
        return

    row = result.data[0]
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

    result = supabase.table("stocks").select("*").eq("user_id", user_id).execute()

    if not result.data:
        await update.message.reply_text(
            "📭 You have no stocks tracked yet. Use /add INFY to start."
        )
        return

    await update.message.reply_text("⏳ Fetching live prices for your portfolio...")

    lines = ["📈 *Your Portfolio*\n"]
    for row in result.data:
        stock_name = row["stock_name"]
        entry_price = float(row["entry_price"])
        entry_date = date.fromisoformat(row["entry_date"])
        days_held = (date.today() - entry_date).days

        current_price = get_live_price(stock_name)
        if current_price is None:
            lines.append(f"• {stock_name} — ❌ Price unavailable")
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

# --- /remove STOCKNAME ---
async def remove_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text("❌ Usage: /remove INFY")
        return

    stock_name = context.args[0].upper()

    result = supabase.table("stocks").select("*")\
        .eq("user_id", user_id).eq("stock_name", stock_name).execute()

    if not result.data:
        await update.message.reply_text(f"❌ You're not tracking {stock_name}.")
        return

    supabase.table("stocks").delete()\
        .eq("user_id", user_id).eq("stock_name", stock_name).execute()

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