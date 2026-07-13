"""AWS Lambda webhook handler for the Telegram stock tracker bot.

Stdlib only — no pip dependencies, so the deployment package is just this file
plus nse_equity.json. Prices come from the Upstox Market Quote API using a
long-lived Analytics Token (read-only, 1-year validity, no static IP needed).
"""

import gzip
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
UPSTOX_TOKEN = os.environ["UPSTOX_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
DB_URL = f"{SUPABASE_URL}/rest/v1/stocks"
UPSTOX_LTP = "https://api.upstox.com/v3/market-quote/ltp"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

USER_AGENT = "stock-tracker-bot/1.0"

# Upstox only. Its Cloudflare rejects the default "Python-urllib/x.y" signature
# (error 1010, browser_signature_banned), so those calls must look like a browser.
# Do NOT send this to Supabase: it refuses secret keys from browser-like clients
# ("Forbidden use of secret API key in browser") and the bot silently reads back
# an empty portfolio.
UPSTOX_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

DB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

# The force_reply prompts double as our conversation state: when a user replies
# to one, the leading emoji tells us which flow they were in. Keep these in sync
# with the checks in handle_message.
PROMPT_ADD = "➕ Reply to this message with the NSE symbol to add.\n\nExample: RELIANCE"
PROMPT_CHECK = "📊 Reply to this message with the NSE symbol to check.\n\nExample: TCS"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Populated from the baked-in map on first use, refreshed from Upstox only if a
# symbol is missing (e.g. a listing newer than the last deploy). Module-global so
# warm invocations reuse it.
_symbols: dict | None = None


# --- HTTP ---------------------------------------------------------------

def http(method, url, headers=None, body=None, timeout=10, benign=()):
    """Return (status, parsed_json_or_none). Never raises on HTTP error status.

    `benign` lists substrings of expected error bodies, logged at info instead of
    error so that real failures stay visible in CloudWatch.
    """
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, method=method, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        text = raw.decode(errors="replace")
        level = log.info if any(b in text for b in benign) else log.error
        level("HTTP %s %s -> %s %s", method, url.split("?")[0], e.code, text[:300])
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, None
    except Exception as e:
        log.error("HTTP %s %s failed: %s", method, url.split("?")[0], e)
        return 0, None


# --- Telegram -----------------------------------------------------------

# Telegram 400s when an edit would produce identical content — e.g. tapping the
# view button you're already on. Nothing is wrong, so don't log it as an error.
TG_BENIGN = ("message is not modified",)


def tg(method, **payload):
    body = json.dumps(payload).encode()
    return http("POST", f"{TELEGRAM_API}/{method}",
                {"Content-Type": "application/json"}, body, benign=TG_BENIGN)


def send(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg("sendMessage", **payload)


def ask(chat_id, prompt, placeholder):
    send(chat_id, prompt, {"force_reply": True, "input_field_placeholder": placeholder})


# --- Supabase -----------------------------------------------------------

ACTIVE, EXITED = "active", "exited"


def db_select(user_id, stock_name=None, status=None):
    params = {"select": "*", "user_id": f"eq.{user_id}", "order": "entry_date.asc"}
    if stock_name:
        params["stock_name"] = f"eq.{stock_name}"
    if status:
        params["status"] = f"eq.{status}"
    _, data = http("GET", f"{DB_URL}?{urllib.parse.urlencode(params)}", DB_HEADERS)
    return data if isinstance(data, list) else []


def db_insert(row):
    http("POST", DB_URL, DB_HEADERS, json.dumps(row).encode())


def db_update(user_id, stock_name, patch, status=None):
    params = {"user_id": f"eq.{user_id}", "stock_name": f"eq.{stock_name}"}
    if status:
        params["status"] = f"eq.{status}"
    http("PATCH", f"{DB_URL}?{urllib.parse.urlencode(params)}",
         DB_HEADERS, json.dumps(patch).encode())


def db_delete(user_id, row_id):
    # By row id, not symbol: re-entering an exited stock leaves two rows with the
    # same stock_name, and deleting by name would take the exit history with it.
    params = {"user_id": f"eq.{user_id}", "id": f"eq.{row_id}"}
    http("DELETE", f"{DB_URL}?{urllib.parse.urlencode(params)}", DB_HEADERS)


# --- Symbols ------------------------------------------------------------

def load_symbols(force_refresh=False):
    """symbol -> instrument_key, e.g. RELIANCE -> NSE_EQ|INE002A01018"""
    global _symbols
    if _symbols is not None and not force_refresh:
        return _symbols

    if not force_refresh:
        try:
            with open(os.path.join(os.path.dirname(__file__), "nse_equity.json")) as f:
                _symbols = json.load(f)
                return _symbols
        except Exception as e:
            log.warning("baked symbol map unavailable (%s), fetching live", e)

    req = urllib.request.Request(INSTRUMENTS_URL, headers={"User-Agent": UPSTOX_USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        records = json.loads(gzip.decompress(r.read()))
    _symbols = {
        d["trading_symbol"]: d["instrument_key"]
        for d in records
        if d.get("segment") == "NSE_EQ" and d.get("instrument_type") == "EQ"
    }
    log.info("refreshed symbol map: %d NSE equities", len(_symbols))
    return _symbols


def instrument_key(symbol):
    key = load_symbols().get(symbol)
    if key:
        return key
    # Could be a listing newer than our baked map — refetch once before giving up.
    return load_symbols(force_refresh=True).get(symbol)


# --- Prices -------------------------------------------------------------

def get_prices(keys):
    """instrument_keys -> {instrument_key: price}. One call for the whole batch."""
    if not keys:
        return {}

    prices = {}
    for i in range(0, len(keys), 500):  # Upstox caps at 500 instruments per call
        chunk = keys[i:i + 500]
        url = f"{UPSTOX_LTP}?{urllib.parse.urlencode({'instrument_key': ','.join(chunk)})}"
        status, body = http("GET", url, {
            "Accept": "application/json",
            "Authorization": f"Bearer {UPSTOX_TOKEN}",
            "User-Agent": UPSTOX_USER_AGENT,
        })
        if status != 200 or not body or body.get("status") != "success":
            log.error("upstox ltp failed: status=%s body=%s", status, str(body)[:300])
            continue

        # Response is keyed as "NSE_EQ:RELIANCE", not by the instrument_key we
        # sent, so resolve via the instrument_token echoed in each value.
        for entry in (body.get("data") or {}).values():
            token = entry.get("instrument_token")
            # last_price is 0 when the market is closed; cp is the previous close.
            price = entry.get("last_price") or entry.get("cp")
            if token and price:
                prices[token] = round(float(price), 2)

    return prices


def get_price(symbol):
    key = instrument_key(symbol)
    if not key:
        return None
    return get_prices([key]).get(key)


def pct(entry, current):
    return ((current - entry) / entry) * 100


def dot(change):
    return "🟢" if change >= 0 else "🔴"


def signed(change):
    return f"{'+' if change >= 0 else ''}{change:.2f}%"


def price_map(rows):
    """symbol -> live price, for a batch of rows. One Upstox call for all of them."""
    keys = {r["stock_name"]: instrument_key(r["stock_name"]) for r in rows}
    prices = get_prices([k for k in keys.values() if k])
    return {sym: prices.get(key) for sym, key in keys.items()}


def summarise(changes):
    """(count, avg, best_symbol, best_pct, worst_symbol, worst_pct) from [(symbol, pct)]"""
    if not changes:
        return 0, None, None, None, None, None
    avg = sum(c for _, c in changes) / len(changes)
    best = max(changes, key=lambda x: x[1])
    worst = min(changes, key=lambda x: x[1])
    return len(changes), avg, best[0], best[1], worst[0], worst[1]


# --- Views --------------------------------------------------------------
# Each returns (text, inline_keyboard). The three views are interchangeable via
# the buttons at the bottom, all editing the same message in place.

NAV = [[
    {"text": "📊 Overall", "callback_data": "view:dash"},
    {"text": "📈 Active", "callback_data": "view:active"},
    {"text": "📕 Exited", "callback_data": "view:exited"},
]]


def active_changes(rows):
    """[(symbol, unrealised %)] for active rows, skipping ones we can't price."""
    prices = price_map(rows)
    out = []
    for r in rows:
        current = prices.get(r["stock_name"])
        if current is not None:
            out.append((r["stock_name"], pct(float(r["entry_price"]), current)))
    return out, prices


def exited_changes(rows):
    """[(symbol, realised %)] for exited rows."""
    return [
        (r["stock_name"], pct(float(r["entry_price"]), float(r["exit_price"])))
        for r in rows if r.get("exit_price") is not None
    ]


def view_dashboard(user_id):
    rows = db_select(user_id)
    active = [r for r in rows if r.get("status", ACTIVE) == ACTIVE]
    exited = [r for r in rows if r.get("status") == EXITED]

    if not rows:
        return "📭 Nothing tracked yet.\n\nUse /add to start tracking a stock!", None

    a_changes, _ = active_changes(active)
    e_changes = exited_changes(exited)

    lines = ["📊 *Portfolio Dashboard*\n"]

    n, avg, best, best_p, worst, worst_p = summarise(a_changes)
    lines.append("📈 *Active* — " + (f"{n} stock{'s' if n != 1 else ''}" if n else "none"))
    if n:
        lines.append(f"   {dot(avg)} Avg unrealised: *{signed(avg)}*")
        lines.append(f"   🏆 Best: {best} {signed(best_p)}")
        if n > 1:
            lines.append(f"   🐌 Worst: {worst} {signed(worst_p)}")
    lines.append("")

    n, avg, best, best_p, worst, worst_p = summarise(e_changes)
    lines.append("📕 *Exited* — " + (f"{n} stock{'s' if n != 1 else ''}" if n else "none"))
    if n:
        lines.append(f"   {dot(avg)} Avg realised: *{signed(avg)}*")
        lines.append(f"   🏆 Best: {best} {signed(best_p)}")
        if n > 1:
            lines.append(f"   🐌 Worst: {worst} {signed(worst_p)}")
    lines.append("")

    both = a_changes + e_changes
    if both:
        overall = sum(c for _, c in both) / len(both)
        wins = sum(1 for _, c in both if c >= 0)
        lines.append(f"*Overall* — {len(both)} positions")
        lines.append(f"   {dot(overall)} Avg return: *{signed(overall)}*")
        lines.append(f"   ✅ {wins} up · ❌ {len(both) - wins} down")

    return "\n".join(lines), NAV


def view_active(user_id):
    rows = db_select(user_id, status=ACTIVE)
    if not rows:
        return "📭 No active stocks.\n\nUse /add to start tracking one!", NAV

    prices = price_map(rows)
    lines = ["📈 *Active Positions*\n"]
    for r in rows:
        symbol = r["stock_name"]
        entry = float(r["entry_price"])
        entry_date = date.fromisoformat(r["entry_date"])
        current = prices.get(symbol)
        days = (date.today() - entry_date).days
        if current is None:
            lines.append(f"• *{symbol}* — ❌ Price unavailable\n")
            continue
        change = pct(entry, current)
        lines.append(
            f"{dot(change)} *{symbol}*  {signed(change)}\n"
            f"   ₹{entry} → ₹{current} · {days}d held\n"
        )
    return "\n".join(lines), NAV


def view_exited(user_id):
    rows = db_select(user_id, status=EXITED)
    if not rows:
        return ("📕 *Exited Positions*\n\nNothing exited yet.\n\n"
                "When you sell a stock, use /exit — it stays here with your realised "
                "profit or loss instead of being deleted."), NAV

    lines = ["📕 *Exited Positions*\n"]
    for r in rows:
        symbol = r["stock_name"]
        entry = float(r["entry_price"])
        exit_price = r.get("exit_price")
        if exit_price is None:
            continue
        exit_price = float(exit_price)
        change = pct(entry, exit_price)
        entry_date = date.fromisoformat(r["entry_date"])
        exit_date = date.fromisoformat(r["exit_date"])
        held = (exit_date - entry_date).days
        lines.append(
            f"{dot(change)} *{symbol}*  {signed(change)}\n"
            f"   ₹{entry} → ₹{exit_price} · held {held}d\n"
            f"   Exited {exit_date.strftime('%d %b %Y')}\n"
        )
    return "\n".join(lines), NAV


# --- Commands -----------------------------------------------------------

HELP = (
    "📖 *Stock Tracker Bot*\n\n"
    "/portfolio — Dashboard: active, exited & overall\n"
    "/add — Track a new stock\n"
    "/check — Check one stock's % change\n"
    "/exit — Mark a stock as sold (keeps it in history)\n"
    "/remove — Delete a stock permanently\n"
    "/help — Show this message\n\n"
    "💡 `/add INFY` works directly too.\n"
    "💡 /exit records today's live price as your sell price."
)


def do_add(chat_id, user_id, symbol):
    symbol = symbol.strip().upper()

    active = db_select(user_id, symbol, status=ACTIVE)
    if active:
        send(chat_id, f"⚠️ Already tracking *{symbol}* at ₹{active[0]['entry_price']}\n"
                      f"Use /exit or /remove first.")
        return

    price = get_price(symbol)
    if price is None:
        send(chat_id, f"❌ Couldn't get a price for *{symbol}*.\n"
                      f"Check the NSE symbol and try /add again.")
        return

    # A previously exited row for the same symbol is left alone — it's history.
    # Re-adding creates a fresh active position, so you can re-enter a stock and
    # keep the record of the old trade.
    db_insert({
        "user_id": user_id, "stock_name": symbol, "exchange": "NSE",
        "entry_price": price, "entry_date": str(date.today()), "status": ACTIVE,
    })
    send(chat_id, f"✅ *{symbol}* added!\n"
                  f"📌 Entry Price: ₹{price}\n"
                  f"📅 {date.today().strftime('%d %b %Y')}\n\n"
                  f"Use /portfolio to see your dashboard.")


def do_check(chat_id, user_id, symbol):
    symbol = symbol.strip().upper()
    rows = db_select(user_id, symbol, status=ACTIVE)
    if not rows:
        send(chat_id, f"❌ Not actively tracking *{symbol}*.\nUse `/add {symbol}` to start.")
        return

    row = rows[0]
    entry = float(row["entry_price"])
    entry_date = date.fromisoformat(row["entry_date"])
    current = get_price(symbol)
    if current is None:
        send(chat_id, f"❌ Could not fetch a price for *{symbol}* right now.")
        return

    change = pct(entry, current)
    send(chat_id,
         f"📊 *{symbol}*\n\n"
         f"📌 Entry: ₹{entry}\n"
         f"💹 Current: ₹{current}\n"
         f"{dot(change)} Change: {signed(change)}\n"
         f"📅 Added {entry_date.strftime('%d %b %Y')} "
         f"({(date.today() - entry_date).days} days ago)")


def do_portfolio(chat_id, user_id):
    text, keyboard = view_dashboard(user_id)
    send(chat_id, text, {"inline_keyboard": keyboard} if keyboard else None)


# --- Multi-select menus -------------------------------------------------
# Lambda keeps no state between taps, so the selection lives in the message:
# Telegram hands the current inline keyboard back on every callback, which means
# the ☐/☑ marks in the button labels ARE the state. Toggling just flips a mark
# and re-renders the keyboard — no database, no session store.

OFF, ON = "☐", "☑"
VERB = {"del": "🗑️ Delete", "exit": "💰 Exit"}


def select_row(mode, row):
    suffix = " (exited)" if mode == "del" and row.get("status") == EXITED else ""
    return [{"text": f"{OFF} {row['stock_name']}{suffix}",
             "callback_data": f"tg:{row['id']}"}]


def footer(mode, count):
    label = f"{VERB[mode]} ({count})" if count else f"{VERB[mode]}"
    return [
        {"text": "✅ All", "callback_data": f"all:{mode}"},
        {"text": label, "callback_data": f"go:{mode}"},
        {"text": "❌ Cancel", "callback_data": "cancel"},
    ]


def selected(keyboard):
    """[(row_id, symbol)] for every ticked button in a selection keyboard."""
    out = []
    for row in keyboard:
        btn = row[0]
        if len(row) == 1 and btn["text"].startswith(ON):
            symbol = btn["text"][1:].strip().replace(" (exited)", "")
            out.append((btn["callback_data"].split(":", 1)[1], symbol))
    return out


def rerender(keyboard, mode):
    """Recount ticks and refresh the footer's count."""
    keyboard[-1] = footer(mode, len(selected(keyboard)))
    return keyboard


def do_exit_menu(chat_id, user_id):
    rows = db_select(user_id, status=ACTIVE)
    if not rows:
        send(chat_id, "📭 No active stocks to exit.")
        return
    keyboard = [select_row("exit", r) for r in rows] + [footer("exit", 0)]
    send(chat_id, "💰 *Which stocks did you sell?*\n\n"
                  "Tap to select as many as you like, then hit Exit.\n"
                  "Today's live price is recorded as your exit price.",
         {"inline_keyboard": keyboard})


def do_remove_menu(chat_id, user_id):
    rows = db_select(user_id)
    if not rows:
        send(chat_id, "📭 Nothing tracked yet.")
        return
    keyboard = [select_row("del", r) for r in rows] + [footer("del", 0)]
    send(chat_id, "🗑️ *Delete permanently — which ones?*\n\n"
                  "Tap to select as many as you like, then hit Delete.\n"
                  "⚠️ This erases them completely. If you sold them, use /exit "
                  "instead so they stay in your history.",
         {"inline_keyboard": keyboard})


def do_exit_many(user_id, picks):
    """Exit several stocks at once, pricing them all in one Upstox call."""
    symbols = [s for _, s in picks]
    keys = {s: instrument_key(s) for s in symbols}
    prices = get_prices([k for k in keys.values() if k])

    done, failed, lines = [], [], []
    today = date.today()
    for row_id, symbol in picks:
        rows = db_select(user_id, symbol, status=ACTIVE)
        price = prices.get(keys.get(symbol))
        if not rows or price is None:
            failed.append(symbol)
            continue
        entry = float(rows[0]["entry_price"])
        db_update(user_id, symbol, {
            "status": EXITED, "exit_price": price, "exit_date": str(today),
        }, status=ACTIVE)
        change = pct(entry, price)
        done.append(change)
        lines.append(f"{dot(change)} *{symbol}*  {signed(change)}\n"
                     f"   ₹{entry} → ₹{price}\n")

    if not done:
        return ("❌ Couldn't fetch live prices, so nothing was exited.\n"
                "Nothing changed — try again in a moment.")

    avg = sum(done) / len(done)
    head = f"✅ *Exited {len(done)} stock{'s' if len(done) != 1 else ''}*\n\n"
    tail = f"\n{dot(avg)} Avg realised: *{signed(avg)}*"
    if failed:
        tail += f"\n\n⚠️ Skipped (no live price): {', '.join(failed)}"
    return head + "\n".join(lines) + tail + "\n\nSee 📕 Exited in /portfolio."


def do_delete_many(user_id, picks):
    for row_id, _ in picks:
        db_delete(user_id, row_id)
    names = ", ".join(f"*{s}*" for _, s in picks)
    return f"🗑️ Deleted permanently: {names}"


# --- Routing ------------------------------------------------------------

def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = (msg.get("text") or "").strip()
    if not text:
        return

    if text.startswith("/"):
        parts = text.split()
        command = parts[0].split("@")[0].lower()
        arg = parts[1] if len(parts) > 1 else None

        if command == "/start":
            send(chat_id, "👋 *Welcome to Stock Tracker Bot!*\n\nType `/` to see all commands.")
        elif command == "/help":
            send(chat_id, HELP)
        elif command == "/add":
            do_add(chat_id, user_id, arg) if arg else ask(chat_id, PROMPT_ADD, "RELIANCE")
        elif command == "/check":
            do_check(chat_id, user_id, arg) if arg else ask(chat_id, PROMPT_CHECK, "TCS")
        elif command == "/portfolio":
            do_portfolio(chat_id, user_id)
        elif command == "/exit":
            do_exit_menu(chat_id, user_id)
        elif command == "/remove":
            do_remove_menu(chat_id, user_id)
        else:
            send(chat_id, "🤔 Unknown command. Try /help")
        return

    # A bare symbol only means something if it replies to one of our prompts.
    replied = (msg.get("reply_to_message") or {}).get("text", "")
    if replied.startswith("➕"):
        do_add(chat_id, user_id, text)
    elif replied.startswith("📊"):
        do_check(chat_id, user_id, text)


def handle_callback(cb):
    data = cb.get("data", "")
    user_id = cb["from"]["id"]
    msg = cb["message"]
    chat_id, message_id = msg["chat"]["id"], msg["message_id"]
    keyboard = (msg.get("reply_markup") or {}).get("inline_keyboard", [])

    def answer(text=None, alert=False):
        payload = {"callback_query_id": cb["id"]}
        if text:
            payload.update(text=text, show_alert=alert)
        tg("answerCallbackQuery", **payload)

    def edit(text, kb=None):
        payload = {"chat_id": chat_id, "message_id": message_id,
                   "text": text, "parse_mode": "Markdown"}
        if kb:
            payload["reply_markup"] = {"inline_keyboard": kb}
        tg("editMessageText", **payload)

    # Tick/untick one stock. Only the keyboard changes, so this is a cheap
    # editMessageReplyMarkup with no database or price lookup at all.
    if data.startswith("tg:"):
        mode = keyboard[-1][1]["callback_data"].split(":", 1)[1]
        for row in keyboard[:-1]:
            btn = row[0]
            if btn["callback_data"] == data:
                on = btn["text"].startswith(ON)
                btn["text"] = (OFF if on else ON) + btn["text"][1:]
        answer()
        tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
           reply_markup={"inline_keyboard": rerender(keyboard, mode)})

    # Select all / clear all.
    elif data.startswith("all:"):
        mode = data.split(":", 1)[1]
        turn_on = len(selected(keyboard)) < len(keyboard) - 1
        for row in keyboard[:-1]:
            row[0]["text"] = (ON if turn_on else OFF) + row[0]["text"][1:]
        answer()
        tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
           reply_markup={"inline_keyboard": rerender(keyboard, mode)})

    elif data.startswith("go:"):
        mode = data.split(":", 1)[1]
        picks = selected(keyboard)
        if not picks:
            answer("Tap the stocks you want first.", alert=True)
            return
        answer()
        edit(do_delete_many(user_id, picks) if mode == "del"
             else do_exit_many(user_id, picks), NAV if mode == "exit" else None)

    elif data.startswith("view:"):
        answer()
        which = data.split(":", 1)[1]
        text, kb = {"dash": view_dashboard, "active": view_active,
                    "exited": view_exited}[which](user_id)
        edit(text, kb)

    elif data == "cancel":
        answer()
        edit("❌ Cancelled.")

    else:
        answer()


def lambda_handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if WEBHOOK_SECRET and headers.get("x-telegram-bot-api-secret-token") != WEBHOOK_SECRET:
        log.warning("rejected request with bad/missing secret token")
        return {"statusCode": 403, "body": "forbidden"}

    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode()

    try:
        update = json.loads(body)
        if "message" in update:
            handle_message(update["message"])
        elif "callback_query" in update:
            handle_callback(update["callback_query"])
    except Exception:
        # Always 200: a non-200 makes Telegram redeliver the same update forever.
        log.exception("failed to handle update")

    return {"statusCode": 200, "body": "ok"}
