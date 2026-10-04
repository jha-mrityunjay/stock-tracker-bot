"""AWS Lambda webhook handler for the Telegram stock tracker bot.

Stdlib only — no pip dependencies, so the deployment package is just this file
plus nse_equity.json. Prices come from the Upstox Market Quote API using a
long-lived Analytics Token (read-only, 1-year validity, no static IP needed).
"""

import gzip
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
# Imported by name, not as `http.client`: the module would shadow our http()
# function, which every caller and every test already relies on.
from http.client import HTTPSConnection

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
UPSTOX_TOKEN = os.environ["UPSTOX_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
DB_URL = f"{SUPABASE_URL}/rest/v1/stocks"
UPSTOX_LTP = "https://api.upstox.com/v3/market-quote/ltp"
UPSTOX_CONTRACTS = "https://api.upstox.com/v2/option/contract"
UPSTOX_CHAIN = "https://api.upstox.com/v2/option/chain"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

EQUITY, OPTION = "equity", "option"

# Owned vs merely watched. A watchlist row is a paper position: it records the
# price when you started watching and tracks the move from there, but is kept out
# of every portfolio P&L number. Options are portfolio-only — a watched option
# would get auto-settled at expiry, which makes no sense for something you never
# bought.
PORTFOLIO, WATCHLIST = "portfolio", "watchlist"

# Tradeable NSE equity series. "EQ" alone misses ~900 real stocks — HILINFRA is
# listed BE (trade-to-trade), SME names are SM/ST, REITs are RR, InvITs are IV.
# Must stay in step with build_instruments.py, or the baked map and the on-demand
# live refresh would cover different universes.
EQUITY_SERIES = {"EQ", "BE", "BZ", "SM", "ST", "IV", "RR"}

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
PROMPT_UNDERLYING = ("🎯 Reply with the underlying to trade options on.\n\n"
                     "Examples: NIFTY, BANKNIFTY, RELIANCE")
PROMPT_WATCH = ("👀 Reply with the NSE symbol to watch.\n\n"
                "You don't own it — the bot records today's price and tracks the move.")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Populated from the baked-in maps on first use, refreshed from Upstox only if a
# symbol is missing (e.g. a listing newer than the last deploy). Module-global so
# warm invocations reuse them.
_symbols: dict | None = None
_underlyings: dict | None = None


# --- HTTP ---------------------------------------------------------------
# Connections are pooled and kept alive across invocations, which is the single
# biggest speed win available here. urllib opens a fresh socket per request, and
# the TLS handshake alone costs ~62ms to Supabase, ~68ms to Upstox and ~417ms to
# Telegram — so a /portfolio was paying well over half a second just shaking
# hands. A warm Lambda container holds these sockets open between invocations, so
# in practice we handshake once and then reuse.

_pool: dict[str, list] = {}
_pool_lock = threading.Lock()

# Independent calls run concurrently on this. Module-level so warm containers
# reuse the threads instead of paying to spawn them each time.
_exec = ThreadPoolExecutor(max_workers=8)


def _take(host):
    with _pool_lock:
        conns = _pool.get(host)
        if conns:
            return conns.pop()
    return HTTPSConnection(host, timeout=10)


def _give(host, conn):
    with _pool_lock:
        _pool.setdefault(host, []).append(conn)


def request(method, url, headers=None, body=None, benign=()):
    """Return (status, parsed_json_or_none). Never raises.

    `benign` lists substrings of expected error bodies, logged at info instead of
    error so that real failures stay visible in CloudWatch.
    """
    parts = urllib.parse.urlsplit(url)
    # The Telegram token is part of the URL path; never write it to CloudWatch.
    shown = url.split("?")[0].replace(TELEGRAM_TOKEN, "<token>")
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    hdrs = {"User-Agent": USER_AGENT, "Connection": "keep-alive", **(headers or {})}

    # Two attempts: a pooled socket can be closed by the far end while our
    # container was frozen, and we only discover that when we try to use it.
    for attempt in (1, 2):
        conn = _take(parts.netloc)
        try:
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            _give(parts.netloc, conn)

            if resp.status >= 400:
                text = raw.decode(errors="replace")
                level = log.info if any(b in text for b in benign) else log.error
                level("HTTP %s %s -> %s %s", method, shown, resp.status, text[:300])
            try:
                return resp.status, (json.loads(raw) if raw else None)
            except Exception:
                return resp.status, None

        except Exception as e:
            try:
                conn.close()          # do NOT return a broken socket to the pool
            except Exception:
                pass
            if attempt == 2:
                log.error("HTTP %s %s failed: %s", method, shown, e)
                return 0, None


# Kept as the name every caller already uses, and as the seam the tests stub.
def http(method, url, headers=None, body=None, timeout=10, benign=()):
    return request(method, url, headers, body, benign)


# --- Cache --------------------------------------------------------------
# Swiping between the Overall / Active / Exited tabs used to re-query Supabase
# AND Upstox on every single tap, and the option picker refetched the same chain
# at each of its three steps. These TTLs are short enough that nothing looks
# stale (a price is at most a few seconds old) but long enough that a burst of
# taps is served from memory. Only lives as long as the warm container.

_cache: dict = {}


def cached(key, ttl, produce):
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = produce()
    _cache[key] = (now + ttl, value)
    return value


def invalidate_user(user_id):
    """Drop a user's cached rows after any write, so they never see stale data."""
    for k in [k for k in _cache if k[0] == "db" and k[1] == user_id]:
        _cache.pop(k, None)


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


def edit_msg(chat_id, message_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "message_id": message_id,
               "text": text, "parse_mode": "Markdown"}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    tg("editMessageText", **payload)


# --- Supabase -----------------------------------------------------------

ACTIVE, EXITED = "active", "exited"


DB_TTL = 4        # seconds; long enough to cover a burst of taps


def db_select(user_id, stock_name=None, status=None):
    def fetch():
        params = {"select": "*", "user_id": f"eq.{user_id}", "order": "entry_date.asc"}
        if stock_name:
            params["stock_name"] = f"eq.{stock_name}"
        if status:
            params["status"] = f"eq.{status}"
        _, data = http("GET", f"{DB_URL}?{urllib.parse.urlencode(params)}", DB_HEADERS)
        return data if isinstance(data, list) else []

    return cached(("db", user_id, stock_name, status), DB_TTL, fetch)


def positions(user_id, status=None, symbol=None, bucket=PORTFOLIO):
    """A user's rows, filtered in memory.

    Always issues the same unfiltered query so every tab shares one cache entry.
    Asking Supabase separately per filter meant each tab switch was its own round
    trip, which is what made the UI feel slow.

    bucket defaults to PORTFOLIO so that no existing caller can accidentally start
    counting watchlist rows in a P&L number. Pass bucket=None for everything.
    """
    rows = db_select(user_id)
    if bucket:
        rows = [r for r in rows if r.get("bucket", PORTFOLIO) == bucket]
    if status:
        rows = [r for r in rows if r.get("status", ACTIVE) == status]
    if symbol:
        rows = [r for r in rows if r["stock_name"] == symbol]
    return rows


def db_insert(row):
    http("POST", DB_URL, DB_HEADERS, json.dumps(row).encode())
    invalidate_user(row["user_id"])


def db_update(user_id, stock_name, patch, status=None):
    params = {"user_id": f"eq.{user_id}", "stock_name": f"eq.{stock_name}"}
    if status:
        params["status"] = f"eq.{status}"
    http("PATCH", f"{DB_URL}?{urllib.parse.urlencode(params)}",
         DB_HEADERS, json.dumps(patch).encode())
    invalidate_user(user_id)


def db_delete(user_id, row_id):
    # By row id, not symbol: re-entering an exited stock leaves two rows with the
    # same stock_name, and deleting by name would take the exit history with it.
    params = {"user_id": f"eq.{user_id}", "id": f"eq.{row_id}"}
    http("DELETE", f"{DB_URL}?{urllib.parse.urlencode(params)}", DB_HEADERS)
    invalidate_user(user_id)


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
        if d.get("segment") == "NSE_EQ" and d.get("instrument_type") in EQUITY_SERIES
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

PRICE_TTL = 5     # seconds; a price this fresh is indistinguishable from live


def get_prices(keys):
    """instrument_keys -> {instrument_key: price}. One call for the whole batch."""
    if not keys:
        return {}
    return cached(("px", tuple(sorted(keys))), PRICE_TTL, lambda: _fetch_prices(keys))


def _fetch_prices(keys):
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


# --- Options ------------------------------------------------------------

def load_underlyings():
    """NIFTY -> NSE_INDEX|Nifty 50, RELIANCE -> NSE_EQ|INE002A01018, ..."""
    global _underlyings
    if _underlyings is None:
        with open(os.path.join(os.path.dirname(__file__), "nse_underlyings.json")) as f:
            _underlyings = json.load(f)
    return _underlyings


def upstox_get(url, params):
    status, body = http("GET", f"{url}?{urllib.parse.urlencode(params)}", {
        "Accept": "application/json",
        "Authorization": f"Bearer {UPSTOX_TOKEN}",
        "User-Agent": UPSTOX_USER_AGENT,
    })
    if status != 200 or not body or body.get("status") != "success":
        log.error("upstox %s failed: %s %s", url, status, str(body)[:200])
        return None
    return body.get("data")


def option_expiries(underlying_key, limit=8):
    """Upcoming expiry dates for an underlying, soonest first."""
    data = cached(("exp", underlying_key), 300, lambda: upstox_get(
        UPSTOX_CONTRACTS, {"instrument_key": underlying_key}))
    if not data:
        return []
    today = date.today()
    expiries = sorted({c["expiry"] for c in data if c.get("expiry")})
    return [e for e in expiries if date.fromisoformat(e) >= today][:limit]


CHAIN_TTL = 10    # seconds; the picker walks the same chain 3 times in a row


def option_chain(underlying_key, expiry):
    """[strike rows] with LTP + instrument_key for both CE and PE, plus spot."""
    return cached(("chain", underlying_key, expiry), CHAIN_TTL, lambda: upstox_get(
        UPSTOX_CHAIN, {"instrument_key": underlying_key, "expiry_date": expiry}) or [])


def find_contract(chain, strike, opt_type):
    """(instrument_key, premium) for one strike+type out of a chain."""
    field = "call_options" if opt_type == "CE" else "put_options"
    for row in chain:
        if abs(float(row["strike_price"]) - float(strike)) < 0.01:
            leg = row.get(field) or {}
            return leg.get("instrument_key"), (leg.get("market_data") or {}).get("ltp")
    return None, None


def spot_of(chain):
    return float(chain[0]["underlying_spot_price"]) if chain else None


def option_label(underlying, strike, opt_type, expiry):
    """NIFTY 24000 CE 14 Jul 26 — matches Upstox's own trading_symbol style."""
    d = date.fromisoformat(expiry) if isinstance(expiry, str) else expiry
    return (f"{underlying} {fmt_strike(strike)} {opt_type} "
            f"{d.strftime('%d %b %y').upper()}")


def fmt_strike(strike):
    s = float(strike)
    return str(int(s)) if s.is_integer() else str(s)


def intrinsic(spot, strike, opt_type):
    """What an option is worth at expiry: its intrinsic value, floored at zero."""
    spot, strike = float(spot), float(strike)
    return round(max(0.0, spot - strike if opt_type == "CE" else strike - spot), 2)


def pct(entry, current):
    return ((current - entry) / entry) * 100


def dot(change):
    return "🟢" if change >= 0 else "🔴"


def signed(change):
    return f"{'+' if change >= 0 else ''}{change:.2f}%"


def row_key(row):
    """Upstox instrument_key for a position.

    Options always store theirs (they aren't in the baked equity map). Equities
    added before that column existed fall back to a symbol lookup.
    """
    return row.get("instrument_key") or instrument_key(row["stock_name"])


def price_map(rows):
    """row id -> live price. One batched Upstox call covering options and equities.

    Keyed by id, not symbol: re-entering a stock leaves two rows sharing a symbol,
    and every option on the same underlying would otherwise collide too.
    """
    keys = {r["id"]: row_key(r) for r in rows}
    prices = get_prices([k for k in keys.values() if k])
    return {rid: prices.get(key) for rid, key in keys.items()}


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

NAV = [
    [
        {"text": "📊 Overall", "callback_data": "view:dash"},
        {"text": "📈 Active", "callback_data": "view:active"},
    ],
    [
        {"text": "📕 Exited", "callback_data": "view:exited"},
        {"text": "👀 Watchlist", "callback_data": "view:watch"},
    ],
]


def dte(row):
    """Days to expiry for an option, or None for an equity."""
    if row.get("kind") != OPTION or not row.get("expiry"):
        return None
    return (date.fromisoformat(row["expiry"]) - date.today()).days


def expiry_tag(row):
    """' · 3d to expiry' — with a warning as it gets close."""
    d = dte(row)
    if d is None:
        return ""
    if d < 0:
        return " · ⏰ EXPIRED"
    if d == 0:
        return " · ⚠️ EXPIRES TODAY"
    return f" · ⏳ {d}d to expiry" + (" ⚠️" if d <= 2 else "")


def active_changes(rows):
    """[(symbol, unrealised %)] for active rows, skipping ones we can't price."""
    prices = price_map(rows)
    out = []
    for r in rows:
        current = prices.get(r["id"])
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
    rows = positions(user_id)
    active = [r for r in rows if r.get("status", ACTIVE) == ACTIVE]
    exited = [r for r in rows if r.get("status") == EXITED]

    if not rows:
        return "📭 Nothing tracked yet.\n\nUse /add to start tracking a stock!", None

    a_changes, _ = active_changes(active)
    e_changes = exited_changes(exited)

    lines = ["📊 *Portfolio Dashboard*\n"]

    n, avg, best, best_p, worst, worst_p = summarise(a_changes)
    lines.append("📈 *Active* — " + (f"{n} position{'s' if n != 1 else ''}" if n else "none"))
    if n:
        lines.append(f"   {dot(avg)} Avg unrealised: *{signed(avg)}*")
        lines.append(f"   🏆 Best: {best} {signed(best_p)}")
        if n > 1:
            lines.append(f"   🐌 Worst: {worst} {signed(worst_p)}")
    lines.append("")

    n, avg, best, best_p, worst, worst_p = summarise(e_changes)
    lines.append("📕 *Exited* — " + (f"{n} position{'s' if n != 1 else ''}" if n else "none"))
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
        lines.append(f"*Overall* — {len(both)} position{'s' if len(both) != 1 else ''}")
        lines.append(f"   {dot(overall)} Avg return: *{signed(overall)}*")
        lines.append(f"   ✅ {wins} up · ❌ {len(both) - wins} down")

    # Watched names are reported separately and never folded into the numbers
    # above — you don't own them, so they aren't your returns.
    watch = positions(user_id, bucket=WATCHLIST)
    if watch:
        w_changes, _ = active_changes(watch)
        n, avg, best, best_p, _, _ = summarise(w_changes)
        lines.append("")
        lines.append(f"👀 *Watchlist* — {len(watch)} stock{'s' if len(watch) != 1 else ''}")
        if n:
            lines.append(f"   {dot(avg)} Avg move since added: *{signed(avg)}*")
            lines.append(f"   🏆 Best: {best} {signed(best_p)}")

    return "\n".join(lines), NAV


def pager(prefix, page, pages):
    """◀ 2/6 ▶ row. Callbacks are f"{prefix}:{page}"; the middle label is inert."""
    row = []
    if page > 0:
        row.append({"text": "◀ Prev", "callback_data": f"{prefix}:{page - 1}"})
    row.append({"text": f"{page + 1}/{pages}", "callback_data": "noop"})
    if page < pages - 1:
        row.append({"text": "Next ▶", "callback_data": f"{prefix}:{page + 1}"})
    return row


def paginate(items, size, page):
    """(slice, page, pages), with page clamped into range."""
    pages = max(1, -(-len(items) // size))
    page = min(max(page, 0), pages - 1)
    return items[page * size:(page + 1) * size], page, pages


# Telegram caps a message at 4096 characters. Each watchlist entry is ~60, so a
# few hundred watched IPOs in one message was a silent 400 and a dead button.
WATCH_PAGE = 40


# "ret": biggest gainers first. "date": most recently added (or listed) first.
WATCH_SORTS = {"ret": "biggest gainers first", "date": "latest first"}


def view_watchlist(user_id, page=0, order="ret"):
    rows = positions(user_id, bucket=WATCHLIST)
    if not rows:
        return ("👀 *Watchlist*\n\nNothing on your watchlist.\n\n"
                "Use /watch to follow a stock you don't own yet — the bot records "
                "today's price and shows you how it moves from here."), NAV

    prices = price_map(rows)
    priced, unpriced = [], []
    for r in rows:
        current = prices.get(r["id"])
        if current is None:
            unpriced.append(r)
        else:
            priced.append((pct(float(r["entry_price"]), current), current, r))
    order = order if order in WATCH_SORTS else "ret"
    ordered = priced + [(None, None, r) for r in unpriced]
    if order == "date":
        # Newest first; id breaks ties between rows added on the same day.
        ordered.sort(key=lambda x: (x[2]["entry_date"], x[2]["id"]), reverse=True)
    else:
        # Biggest movers first, so page 1 is the interesting one.
        ordered.sort(key=lambda x: (x[0] is not None, x[0] or 0), reverse=True)

    chunk, page, pages = paginate(ordered, WATCH_PAGE, page)

    lines = [f"👀 *Watchlist* — {len(rows)} stock{'s' if len(rows) != 1 else ''}",
             "_Not owned — tracked from the day you added them._"]
    if priced:
        avg = sum(c for c, _, _ in priced) / len(priced)
        up = sum(1 for c, _, _ in priced if c >= 0)
        lines.append(f"{dot(avg)} Avg move: *{signed(avg)}* · ✅ {up} up · ❌ {len(priced) - up} down")
    if pages > 1:
        lines.append(f"_Page {page + 1} of {pages} · {WATCH_SORTS[order]}_")
    lines.append("")

    today = date.today()
    for change, current, r in chunk:
        symbol = r["stock_name"]
        since = date.fromisoformat(r["entry_date"])
        when = f"{since.strftime('%d %b %y')} · {(today - since).days}d"
        if current is None:
            lines.append(f"• *{symbol}* — ❌ Price unavailable · {when}\n")
            continue
        lines.append(
            f"{dot(change)} *{symbol}*  {signed(change)}\n"
            f"   ₹{float(r['entry_price'])} → ₹{current} · {when}\n"
        )

    # The active sort is marked; tapping either goes back to page 1.
    keyboard = [
        [{"text": ("✓ " if order == key else "") + label, "callback_data": f"wp:{key}:0"}
         for key, label in (("ret", "📈 Return"), ("date", "📅 Latest"))],
        [{"text": "💰 I bought one", "callback_data": "buymenu"}],
    ]
    if pages > 1:
        keyboard.append(pager(f"wp:{order}", page, pages))
    return "\n".join(lines), keyboard + NAV


def view_active(user_id):
    rows = positions(user_id, status=ACTIVE)
    if not rows:
        return "📭 No active stocks.\n\nUse /add to start tracking one!", NAV

    prices = price_map(rows)
    lines = ["📈 *Active Positions*\n"]
    for r in rows:
        symbol = r["stock_name"]
        entry = float(r["entry_price"])
        entry_date = date.fromisoformat(r["entry_date"])
        current = prices.get(r["id"])
        days = (date.today() - entry_date).days
        icon = "🎯" if r.get("kind") == OPTION else ""
        if current is None:
            lines.append(f"• {icon}*{symbol}* — ❌ Price unavailable\n")
            continue
        change = pct(entry, current)
        lines.append(
            f"{dot(change)} {icon}*{symbol}*  {signed(change)}\n"
            f"   ₹{entry} → ₹{current} · {days}d held{expiry_tag(r)}\n"
        )
    return "\n".join(lines), NAV


def view_exited(user_id):
    rows = positions(user_id, status=EXITED)
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
        icon = "🎯" if r.get("kind") == OPTION else ""
        how = "⏰ Expired" if r.get("exit_reason") == "expiry" else "Exited"
        lines.append(
            f"{dot(change)} {icon}*{symbol}*  {signed(change)}\n"
            f"   ₹{entry} → ₹{exit_price} · held {held}d\n"
            f"   {how} {exit_date.strftime('%d %b %Y')}\n"
        )
    return "\n".join(lines), NAV


# --- Commands -----------------------------------------------------------

HELP = (
    "📖 *Stock Tracker Bot*\n\n"
    "/portfolio — Dashboard: active, exited & overall\n"
    "/add — Track a stock 📈 or an option 🎯\n"
    "/check — Check one stock's % change\n"
    "/exit — Mark positions as sold (keeps them in history)\n"
    "/remove — Delete positions permanently\n"
    "/help — Show this message\n\n"
    "💡 `/add INFY` adds a stock directly.\n"
    "💡 /add with no symbol lets you pick an option: "
    "underlying → expiry → strike → CE/PE.\n"
    "💡 /exit and /remove let you select several at once.\n"
    "⏰ Options auto-settle at expiry at their intrinsic value, and you get a "
    "message when they do."
)


def do_add(chat_id, user_id, symbol, bucket=PORTFOLIO):
    symbol = symbol.strip().upper()
    watching = bucket == WATCHLIST

    existing = positions(user_id, status=ACTIVE, symbol=symbol, bucket=bucket)
    if existing:
        where = "on your watchlist" if watching else "tracking"
        send(chat_id, f"⚠️ Already {where}: *{symbol}* at ₹{existing[0]['entry_price']}\n"
                      f"Use /remove first.")
        return

    price = get_price(symbol)
    if price is None:
        send(chat_id, f"❌ Couldn't get a price for *{symbol}*.\n"
                      f"Check the NSE symbol and try again.")
        return

    # A previously exited row for the same symbol is left alone — it's history.
    # Re-adding creates a fresh active position, so you can re-enter a stock and
    # keep the record of the old trade.
    db_insert({
        "user_id": user_id, "stock_name": symbol, "exchange": "NSE",
        "entry_price": price, "entry_date": str(date.today()), "status": ACTIVE,
        "bucket": bucket,
    })

    if watching:
        send(chat_id, f"👀 Watching *{symbol}*\n"
                      f"📌 Price today: ₹{price}\n\n"
                      f"You don't own this — it won't count in your P&L. "
                      f"When you buy it, hit *I bought one* on the 👀 Watchlist tab.")
    else:
        send(chat_id, f"✅ *{symbol}* added!\n"
                      f"📌 Entry Price: ₹{price}\n"
                      f"📅 {date.today().strftime('%d %b %Y')}\n\n"
                      f"Use /portfolio to see your dashboard.")


def do_buy_menu(chat_id, user_id):
    show_menu(chat_id, user_id, "buy")


def do_buy_many(user_id, picks):
    """Promote watched stocks into real positions at today's price."""
    ids = {str(row_id) for row_id, _ in picks}
    rows = [r for r in positions(user_id, bucket=WATCHLIST) if str(r["id"]) in ids]
    prices = price_map(rows)

    lines, writes, failed = [], [], []
    today = date.today()
    for r in rows:
        symbol = r["stock_name"]
        price = prices.get(r["id"])
        if price is None:
            failed.append(symbol)
            continue
        watched_at = float(r["entry_price"])
        watched_days = (today - date.fromisoformat(r["entry_date"])).days

        # Entry is today's price, not the price you were watching at — you didn't
        # own it back then, so pretending you bought at that level would invent a
        # gain you never made. The old watch price is shown for context only.
        writes.append(_exec.submit(db_update_id, r["id"], {
            "bucket": PORTFOLIO, "status": ACTIVE,
            "entry_price": price, "entry_date": str(today),
        }))
        missed = pct(watched_at, price)
        lines.append(f"✅ *{symbol}* bought at ₹{price}\n"
                     f"   _watched from ₹{watched_at} ({signed(missed)} over {watched_days}d)_\n")

    for w in writes:
        w.result()

    if not lines:
        return ("❌ Couldn't fetch live prices, so nothing was bought.\n"
                "Nothing changed — try again in a moment.")

    tail = f"\n\n⚠️ Skipped (no live price): {', '.join(failed)}" if failed else ""
    return ("💰 *Moved into your portfolio*\n\n" + "\n".join(lines) + tail
            + "\nEntry price is today's — see 📈 Active.")


def do_check(chat_id, user_id, symbol):
    symbol = symbol.strip().upper()
    rows = positions(user_id, status=ACTIVE, symbol=symbol)
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
VERB = {"del": "🗑️ Delete", "exit": "💰 Exit", "buy": "💰 Bought"}


def select_row(mode, row):
    suffix = ""
    if mode == "del":
        if row.get("status") == EXITED:
            suffix = " (exited)"
        elif row.get("bucket") == WATCHLIST:
            suffix = " (watching)"
    return [{"text": f"{OFF} {row['stock_name']}{suffix}",
             "callback_data": f"tg:{row['id']}"}]


def footer(mode, count):
    label = f"{VERB[mode]} ({count})" if count else f"{VERB[mode]}"
    return [
        {"text": "✅ All", "callback_data": f"all:{mode}"},
        {"text": label, "callback_data": f"go:{mode}"},
        {"text": "❌ Cancel", "callback_data": "cancel"},
    ]


def pickable(keyboard):
    """The tickable stock rows — not the pager or the footer."""
    return [row for row in keyboard if row[0]["callback_data"].startswith("tg:")]


def selected(keyboard):
    """[(row_id, symbol)] for every ticked button in a selection keyboard."""
    out = []
    for row in pickable(keyboard):
        btn = row[0]
        if btn["text"].startswith(ON):
            symbol = btn["text"][1:].strip().split(" (")[0]
            out.append((btn["callback_data"].split(":", 1)[1], symbol))
    return out


def rerender(keyboard, mode):
    """Recount ticks and refresh the footer's count."""
    keyboard[-1] = footer(mode, len(selected(keyboard)))
    return keyboard


# Telegram rejects a keyboard of more than ~100 buttons, so long menus page.
# The selection still lives in the keyboard, which means it only spans the page
# you're on: turning the page re-renders it fresh.
MENU_PAGE = 30

# mode -> (rows for this user, message when there are none, prompt)
MENUS = {
    "exit": (lambda uid: positions(uid, status=ACTIVE, bucket=PORTFOLIO),
             "📭 No active stocks to exit.",
             "💰 *Which stocks did you sell?*\n\n"
             "Tap to select as many as you like, then hit Exit.\n"
             "Today's live price is recorded as your exit price."),
    "del": (lambda uid: positions(uid, bucket=None),
            "📭 Nothing tracked yet.",
            "🗑️ *Delete permanently — which ones?*\n\n"
            "Tap to select as many as you like, then hit Delete.\n"
            "⚠️ This erases them completely. If you sold them, use /exit "
            "instead so they stay in your history."),
    "buy": (lambda uid: positions(uid, bucket=WATCHLIST),
            "👀 Nothing on your watchlist to buy.",
            "💰 *Which did you buy?*\n\n"
            "Tap to select, then confirm. Today's live price becomes your "
            "entry price and it moves into your portfolio."),
}


def show_menu(chat_id, user_id, mode, page=0, message_id=None):
    """Send a multi-select menu, or with message_id, turn it to another page."""
    fetch, empty, prompt = MENUS[mode]
    rows = sorted(fetch(user_id), key=lambda r: r["stock_name"])
    if not rows:
        send(chat_id, empty) if message_id is None else edit_msg(chat_id, message_id, empty)
        return

    chunk, page, pages = paginate(rows, MENU_PAGE, page)
    keyboard = [select_row(mode, r) for r in chunk]
    text = prompt
    if pages > 1:
        keyboard.append(pager(f"pg:{mode}", page, pages))
        text += f"\n\n_Page {page + 1} of {pages} · A–Z · selection is per page._"
    keyboard.append(footer(mode, 0))

    if message_id is None:
        send(chat_id, text, {"inline_keyboard": keyboard})
    else:
        edit_msg(chat_id, message_id, text, keyboard)


def do_exit_menu(chat_id, user_id):
    show_menu(chat_id, user_id, "exit")


def do_remove_menu(chat_id, user_id):
    show_menu(chat_id, user_id, "del")


def do_exit_many(user_id, picks):
    """Exit several positions at once, pricing them all in one Upstox call.

    Works off row ids, not symbols: an option's label ("NIFTY 24200 CE 20 JUL 26")
    is not in the equity map, so a symbol lookup would fail to price every option.
    """
    ids = {str(row_id) for row_id, _ in picks}
    rows = [r for r in positions(user_id, status=ACTIVE) if str(r["id"]) in ids]
    prices = price_map(rows)

    done, failed, lines, writes = [], [], [], []
    today = date.today()
    for r in rows:
        symbol = r["stock_name"]
        price = prices.get(r["id"])
        if price is None:
            failed.append(symbol)
            continue
        entry = float(r["entry_price"])
        # Concurrent: exiting 5 positions was 5 sequential round trips to Supabase.
        writes.append(_exec.submit(db_update_id, r["id"], {
            "status": EXITED, "exit_price": price, "exit_date": str(today),
            "exit_reason": "manual",
        }))
        change = pct(entry, price)
        done.append(change)
        icon = "🎯" if r.get("kind") == OPTION else ""
        lines.append(f"{dot(change)} {icon}*{symbol}*  {signed(change)}\n"
                     f"   ₹{entry} → ₹{price}\n")

    for w in writes:
        w.result()

    if not done:
        return ("❌ Couldn't fetch live prices, so nothing was exited.\n"
                "Nothing changed — try again in a moment.")

    avg = sum(done) / len(done)
    head = f"✅ *Exited {len(done)} position{'s' if len(done) != 1 else ''}*\n\n"
    tail = f"\n{dot(avg)} Avg realised: *{signed(avg)}*"
    if failed:
        tail += f"\n\n⚠️ Skipped (no live price): {', '.join(failed)}"
    return head + "\n".join(lines) + tail + "\n\nSee 📕 Exited in /portfolio."


def do_delete_many(user_id, picks):
    futures = [_exec.submit(db_delete, user_id, row_id) for row_id, _ in picks]
    for f in futures:
        f.result()
    names = ", ".join(f"*{s}*" for _, s in picks)
    return f"🗑️ Deleted permanently: {names}"


# --- Option picker ------------------------------------------------------
# Lambda is stateless, so the whole underlying → expiry → strike → CE/PE journey
# is carried in callback_data (Telegram allows 64 bytes; "oi:BANKNIFTY:2026-07-14:
# 58000:CE" is ~32). Nothing is written to the database until the final tap.

def do_add_menu(chat_id):
    send(chat_id, "➕ *What do you want to track?*", {"inline_keyboard": [
        [
            {"text": "📈 Stock I own", "callback_data": "add:eq"},
            {"text": "👀 Just watching", "callback_data": "add:watch"},
        ],
        [{"text": "🎯 Option", "callback_data": "add:opt"}],
    ]})


def do_pick_expiry(chat_id, underlying):
    underlying = underlying.strip().upper()
    key = load_underlyings().get(underlying)
    if not key:
        send(chat_id, f"❌ No options listed on *{underlying}*.\n\n"
                      f"Try NIFTY, BANKNIFTY, FINNIFTY, or an F&O stock like RELIANCE.")
        return

    expiries = option_expiries(key)
    if not expiries:
        send(chat_id, f"❌ Couldn't load expiries for *{underlying}*. Try again shortly.")
        return

    keyboard = [[{"text": date.fromisoformat(e).strftime("%d %b %Y"),
                  "callback_data": f"oe:{underlying}:{e}"}] for e in expiries]
    keyboard.append([{"text": "❌ Cancel", "callback_data": "cancel"}])
    send(chat_id, f"🎯 *{underlying} options*\n\nPick an expiry:",
         {"inline_keyboard": keyboard})


def strike_window(chain, spot, n=6):
    """The n strikes either side of spot — nobody scrolls 99 buttons."""
    strikes = sorted({float(r["strike_price"]) for r in chain})
    if not strikes:
        return []
    atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    return strikes[max(0, atm - n):atm + n + 1]


def do_pick_strike(chat_id, message_id, underlying, expiry):
    key = load_underlyings().get(underlying)
    chain = option_chain(key, expiry)
    spot = spot_of(chain)
    if not chain or spot is None:
        edit_msg(chat_id, message_id, f"❌ Couldn't load the {underlying} chain. Try again.")
        return

    strikes = strike_window(chain, spot)
    atm = min(strikes, key=lambda s: abs(s - spot))

    # Three per row, ATM marked so you can see where the money is.
    buttons, row = [], []
    for s in strikes:
        mark = "🎯" if s == atm else ""
        row.append({"text": f"{mark}{fmt_strike(s)}",
                    "callback_data": f"ok:{underlying}:{expiry}:{fmt_strike(s)}"})
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([{"text": "❌ Cancel", "callback_data": "cancel"}])

    edit_msg(chat_id, message_id,
             f"🎯 *{underlying}* · {date.fromisoformat(expiry).strftime('%d %b %Y')}\n\n"
             f"Spot: *₹{spot}*  (🎯 = at the money)\n\nPick a strike:",
             buttons)


def do_pick_type(chat_id, message_id, underlying, expiry, strike):
    key = load_underlyings().get(underlying)
    chain = option_chain(key, expiry)
    spot = spot_of(chain)
    ce_key, ce = find_contract(chain, strike, "CE")
    pe_key, pe = find_contract(chain, strike, "PE")

    buttons = [[
        {"text": f"📈 CE  ₹{ce}" if ce else "📈 CE",
         "callback_data": f"oi:{underlying}:{expiry}:{strike}:CE"},
        {"text": f"📉 PE  ₹{pe}" if pe else "📉 PE",
         "callback_data": f"oi:{underlying}:{expiry}:{strike}:PE"},
    ], [{"text": "❌ Cancel", "callback_data": "cancel"}]]

    edit_msg(chat_id, message_id,
             f"🎯 *{underlying} {fmt_strike(strike)}* · "
             f"{date.fromisoformat(expiry).strftime('%d %b %Y')}\n\n"
             f"Spot: ₹{spot}\n\nCall or Put?",
             buttons)


def do_add_option(chat_id, user_id, underlying, expiry, strike, opt_type):
    key = load_underlyings().get(underlying)
    chain = option_chain(key, expiry)
    ikey, premium = find_contract(chain, strike, opt_type)

    if not ikey or not premium:
        return (f"❌ Couldn't price *{option_label(underlying, strike, opt_type, expiry)}*.\n"
                f"Nothing was added — try again in a moment.")

    label = option_label(underlying, strike, opt_type, expiry)
    if [r for r in positions(user_id, status=ACTIVE) if r.get("instrument_key") == ikey]:
        return f"⚠️ Already tracking *{label}*.\nUse /exit or /remove first."

    premium = round(float(premium), 2)
    db_insert({
        "user_id": user_id, "stock_name": label, "exchange": "NSE", "kind": OPTION,
        "instrument_key": ikey, "underlying": underlying, "expiry": expiry,
        "strike": float(strike), "option_type": opt_type,
        "entry_price": premium, "entry_date": str(date.today()), "status": ACTIVE,
    })

    days = (date.fromisoformat(expiry) - date.today()).days
    return (f"✅ *{label}* added!\n\n"
            f"📌 Premium: ₹{premium}\n"
            f"⏳ {days} days to expiry\n\n"
            f"It'll auto-settle at expiry — see /portfolio.")


# --- Expiry settlement --------------------------------------------------
# Run daily by an EventBridge cron after the close. An expired option is worth its
# intrinsic value and nothing else, and the contract vanishes from Upstox's
# instrument list the next morning — so if we don't settle it on the day, we lose
# the ability to price it at all and the position rots as "active" forever.

def db_expired_options():
    """Active option positions at or past expiry, across ALL users."""
    params = {"select": "*", "kind": f"eq.{OPTION}", "status": f"eq.{ACTIVE}",
              "expiry": f"lte.{date.today()}"}
    _, data = http("GET", f"{DB_URL}?{urllib.parse.urlencode(params)}", DB_HEADERS)
    return data if isinstance(data, list) else []


def db_update_id(row_id, patch):
    http("PATCH", f"{DB_URL}?{urllib.parse.urlencode({'id': f'eq.{row_id}'})}",
         DB_HEADERS, json.dumps(patch).encode())
    # We don't know whose row this is, so drop every cached read. Without this you
    # could exit a position and still see it listed as active for a few seconds.
    for k in [k for k in _cache if k[0] == "db"]:
        _cache.pop(k, None)


def run_settlement():
    rows = db_expired_options()
    if not rows:
        log.info("settlement: nothing expired")
        return {"settled": 0}

    # One batched call for every underlying involved.
    underlyings = load_underlyings()
    keys = {r["underlying"]: underlyings.get(r["underlying"]) for r in rows}
    spots = get_prices([k for k in keys.values() if k])

    settled, by_user = 0, {}
    for r in rows:
        spot = spots.get(keys.get(r["underlying"]))
        if spot is None:
            log.error("settlement: no spot for %s, leaving %s active",
                      r["underlying"], r["stock_name"])
            continue

        value = intrinsic(spot, r["strike"], r["option_type"])
        db_update_id(r["id"], {
            "status": EXITED, "exit_price": value,
            "exit_date": r["expiry"], "exit_reason": "expiry",
        })
        settled += 1

        change = pct(float(r["entry_price"]), value) if float(r["entry_price"]) else 0.0
        by_user.setdefault(r["user_id"], []).append(
            f"{dot(change)} *{r['stock_name']}*  {signed(change)}\n"
            f"   ₹{r['entry_price']} → ₹{value}"
            + ("  (expired worthless)" if value == 0 else f"  (spot ₹{spot})"))

    for user_id, lines in by_user.items():
        send(user_id, "⏰ *Options expired and settled*\n\n" + "\n\n".join(lines)
                      + "\n\nThey're now in 📕 Exited — see /portfolio.")

    log.info("settlement: settled %d option(s)", settled)
    return {"settled": settled}

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
            do_add(chat_id, user_id, arg) if arg else do_add_menu(chat_id)
        elif command == "/check":
            do_check(chat_id, user_id, arg) if arg else ask(chat_id, PROMPT_CHECK, "TCS")
        elif command == "/portfolio":
            do_portfolio(chat_id, user_id)
        elif command == "/watch":
            (do_add(chat_id, user_id, arg, bucket=WATCHLIST) if arg
             else ask(chat_id, PROMPT_WATCH, "HILINFRA"))
        elif command == "/bought":
            do_buy_menu(chat_id, user_id)
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
    elif replied.startswith("👀"):
        do_add(chat_id, user_id, text, bucket=WATCHLIST)
    elif replied.startswith("🎯"):
        do_pick_expiry(chat_id, text)


def handle_callback(cb):
    # Lambda freezes the container the instant we return, so any concurrent
    # Telegram call must be awaited here or it is silently killed mid-flight.
    pending = []
    try:
        dispatch_callback(cb, pending)
    finally:
        for f in pending:
            f.result()


def dispatch_callback(cb, pending):
    data = cb.get("data", "")
    user_id = cb["from"]["id"]
    msg = cb["message"]
    chat_id, message_id = msg["chat"]["id"], msg["message_id"]
    keyboard = (msg.get("reply_markup") or {}).get("inline_keyboard", [])

    def answer(text=None, alert=False):
        """Ack the tap. Fired off concurrently: it kills Telegram's button spinner
        immediately, and blocking on it would add a round trip in front of every
        database and price lookup we're about to do."""
        payload = {"callback_query_id": cb["id"]}
        if text:
            payload.update(text=text, show_alert=alert)
        pending.append(_exec.submit(tg, "answerCallbackQuery", **payload))

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
        rows = pickable(keyboard)
        turn_on = len(selected(keyboard)) < len(rows)
        for row in rows:
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
        action = {"del": do_delete_many, "exit": do_exit_many, "buy": do_buy_many}[mode]
        edit(action(user_id, picks), None if mode == "del" else NAV)

    elif data == "add:eq":
        answer()
        ask(chat_id, PROMPT_ADD, "RELIANCE")

    elif data == "add:watch":
        answer()
        ask(chat_id, PROMPT_WATCH, "HILINFRA")

    elif data == "buymenu":
        answer()
        do_buy_menu(chat_id, user_id)

    elif data == "add:opt":
        answer()
        ask(chat_id, PROMPT_UNDERLYING, "NIFTY")

    elif data.startswith("oe:"):        # expiry chosen -> show strikes
        answer()
        _, underlying, expiry = data.split(":", 2)
        do_pick_strike(chat_id, message_id, underlying, expiry)

    elif data.startswith("ok:"):        # strike chosen -> show CE / PE
        answer()
        _, underlying, expiry, strike = data.split(":", 3)
        do_pick_type(chat_id, message_id, underlying, expiry, strike)

    elif data.startswith("oi:"):        # CE/PE chosen -> open the position
        answer()
        _, underlying, expiry, strike, opt_type = data.split(":", 4)
        edit(do_add_option(chat_id, user_id, underlying, expiry, strike, opt_type), NAV)

    elif data.startswith("view:"):
        answer()
        which = data.split(":", 1)[1]
        text, kb = {"dash": view_dashboard, "active": view_active,
                    "exited": view_exited, "watch": view_watchlist}[which](user_id)
        edit(text, kb)

    elif data.startswith("wp:"):        # watchlist sort + page: "wp:date:2"
        answer()
        parts = data.split(":")
        # "wp:2" is the pre-sort format, still on buttons in old messages.
        order, page = (parts[1], parts[2]) if len(parts) == 3 else ("ret", parts[1])
        edit(*view_watchlist(user_id, int(page), order))

    elif data.startswith("pg:"):        # multi-select menu page
        answer()
        _, mode, page = data.split(":", 2)
        show_menu(chat_id, user_id, mode, int(page), message_id)

    elif data == "cancel":
        answer()
        edit("❌ Cancelled.")

    else:
        answer()


def ensure_webhook(url):
    """Put our webhook back if something removed it.

    Any polling client on this token (the old Railway bot, a local script) calls
    deleteWebhook when it starts, after which Telegram stops calling us and the
    bot looks dead with nothing in our logs. The warm ping runs this every 5
    minutes, which caps that outage at 5 minutes. Messages sent in the meantime
    are queued by Telegram and delivered once the webhook is back.
    """
    if not url:
        return "skipped"
    status, info = tg("getWebhookInfo")    # also keeps the Telegram socket warm
    if status != 200 or not info:
        return "unknown"
    current = (info.get("result") or {}).get("url", "")
    if current == url:
        return "ok"
    log.warning("webhook was %r, restoring", current or "removed")
    tg("setWebhook", url=url, secret_token=WEBHOOK_SECRET,
       allowed_updates=["message", "callback_query"])
    return "restored"


def lambda_handler(event, context):
    # EventBridge crons, not Telegram: no HTTP envelope, so check these first.
    task = event.get("task")

    if task == "settle":
        return run_settlement()

    if task == "warm":
        # Keeps a container alive so your next tap doesn't pay a cold start, and
        # — the real prize — keeps the pooled TLS connections open. A fresh
        # handshake to Telegram alone costs ~417ms.
        load_symbols()
        load_underlyings()
        return {"warm": True, "webhook": ensure_webhook(event.get("webhook"))}

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
