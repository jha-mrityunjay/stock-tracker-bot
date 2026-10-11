"""Evening scan (EventBridge, weekdays 19:30 IST): IPO breakouts, volume spikes and
spike-day breakouts from Upstox's daily OHLC + volume, with no PC involved.

The memory it needs lives in Supabase Storage (bucket 'swingsys', 'scan_state.json'):
the swing-research system on the owner's PC writes a fresh copy whenever it runs
(src/swingsys/live/scan_state.py), and this scan updates it every evening, so it keeps
working on days the PC is off.

  vol    {symbol: [last 20 daily volumes]}   NIFTY 500 members with ADV >= Rs 1 cr
  ipos   {symbol: {listed, lh, issue, age}}  mainboard IPOs never closed above the listing-day high
  spikes {symbol: {date, high, low, x, age}} >= 5x volume days of the last 20 sessions, range unbroken

Rules match the research system (tested there): a spike is volume >= 5x the previous
20-day average; a spike-day breakout is the first close beyond that day's high/low within
20 sessions (54-59% lean, a hint); an IPO breakout is the first close above the listing-day
high at least 5 sessions after listing (H37, paper; stop = a close below that day's low).
New IPOs listed after the last PC run are not known until the PC runs again.
Nothing here places orders.
"""
from datetime import datetime, timedelta, timezone

import handler as h

BUCKET, OBJECT = "swingsys", "scan_state.json"
UPSTOX_OHLC = "https://api.upstox.com/v3/market-quote/ohlc"
IST = timezone(timedelta(hours=5, minutes=30))
SPIKE_X, LOOKBACK, MIN_ADV = 5.0, 20, 1e7


def _store_headers():
    return {"apikey": h.SUPABASE_KEY, "Authorization": f"Bearer {h.SUPABASE_KEY}"}


def load_state():
    status, data = h.http("GET", f"{h.SUPABASE_URL}/storage/v1/object/{BUCKET}/{OBJECT}", _store_headers())
    return data if status == 200 and isinstance(data, dict) else None


def save_state(state):
    body = h.json.dumps(state, separators=(",", ":")).encode()
    h.http("POST", f"{h.SUPABASE_URL}/storage/v1/object/{BUCKET}/{OBJECT}",
           {**_store_headers(), "Content-Type": "application/json", "x-upsert": "true"}, body)


def daily_bars(symbols):
    """symbol -> today's {open, high, low, close, volume, ts} from Upstox (batches of 500)."""
    m = h.load_symbols()
    if any(s not in m for s in symbols):        # new listings: refresh the map ONCE, not per symbol
        m = h.load_symbols(force_refresh=True)
    keys = {s: m.get(s) for s in symbols}
    by_token = {k: s for s, k in keys.items() if k}
    tokens, out = list(by_token), {}
    for i in range(0, len(tokens), 500):
        chunk = tokens[i:i + 500]
        url = f"{UPSTOX_OHLC}?{h.urllib.parse.urlencode({'instrument_key': ','.join(chunk), 'interval': '1d'})}"
        status, body = h.http("GET", url, {"Accept": "application/json", "Authorization": f"Bearer {h.UPSTOX_TOKEN}",
                                           "User-Agent": h.UPSTOX_USER_AGENT})
        if status != 200 or not body or body.get("status") != "success":
            h.log.error("upstox ohlc failed: status=%s body=%s", status, str(body)[:300])
            continue
        for entry in (body.get("data") or {}).values():
            bar, sym = entry.get("live_ohlc"), by_token.get(entry.get("instrument_token"))
            if bar and sym and bar.get("close"):
                out[sym] = bar
    return out


def scan(state, bars, today):
    """Pure logic (tested): returns (message lines, IPO breakouts [(symbol, close)], new state)."""
    lines, ipo_hits = [], []
    # IPO breakouts: first close above the listing-day high, >= 5 sessions after listing
    for s, ipo in list(state.get("ipos", {}).items()):
        b = bars.get(s)
        if not b:
            continue
        ipo["age"] = ipo.get("age", 0) + 1
        if ipo["age"] >= 5 and b["close"] > ipo["lh"]:
            vs = f", {(b['close'] / ipo['issue'] - 1) * 100:+.0f}% vs issue" if ipo.get("issue") else ""
            lines.append(f"  {s}  close {b['close']:,.2f} > listing high {ipo['lh']:,.2f}{vs}; "
                         f"stop = close below {b['low']:,.2f}")
            ipo_hits.append((s, b["close"]))
            del state["ipos"][s]
    # spike-day breakouts (first close beyond a recent spike day's range)
    bo = []
    for s, sp in list(state.get("spikes", {}).items()):
        b = bars.get(s)
        if not b:
            continue
        sp["age"] = sp.get("age", 0) + 1
        if b["close"] > sp["high"] or b["close"] < sp["low"]:
            up = b["close"] > sp["high"]
            bo.append(f"  {s} {'UP' if up else 'DOWN'}  close {b['close']:,.2f} vs "
                      f"{sp['high'] if up else sp['low']:,.2f} (spike {sp['date'][8:10]}/{sp['date'][5:7]}, {sp['x']}x)")
            del state["spikes"][s]
        elif sp["age"] > LOOKBACK:
            del state["spikes"][s]
    # volume spikes today, then roll the 20-day volumes
    spikes = []
    for s, vols in state.get("vol", {}).items():
        b = bars.get(s)
        if not b:
            continue
        if len(vols) >= 20:
            avg = sum(vols[-20:]) / 20
            if avg and b["volume"] >= SPIKE_X * avg and avg * b["close"] >= MIN_ADV:
                chg = (b["close"] / b["open"] - 1) * 100 if b.get("open") else 0.0
                spikes.append((b["volume"] / avg, s, chg, b))
                state.setdefault("spikes", {})[s] = {"date": today, "high": b["high"], "low": b["low"],
                                                     "x": round(b["volume"] / avg, 1), "age": 0}
        state["vol"][s] = (vols + [int(b["volume"])])[-20:]
    state["as_of"] = today
    msg = []
    if lines:
        msg += ["🚀 IPO BREAKOUT (first close above the listing-day high; H37 paper rules):"] + lines + \
               ["  Buy next open (paper); half at 2x; rest until a close below SMA200."]
    if bo:
        msg += ["", "SPIKE-DAY BREAKOUT (54-59% lean, a hint):"] + bo
    if spikes:
        spikes.sort(reverse=True)
        msg += ["", f"VOLUME SPIKES (>= 5x avg; {len(spikes)}; direction unknown):"]
        msg += [f"  {s}  {chg:+.1f}% (open->close), {x:.1f}x vol; watch above {b['high']:,.2f} / below {b['low']:,.2f}"
                for x, s, chg, b in spikes[:15]]
    while msg and not msg[0]:
        msg.pop(0)
    return msg, ipo_hits, state


def run_scan(user_id):
    today = datetime.now(IST).date().isoformat()
    state = load_state()
    if not state:
        h.send(user_id, "⚡ Evening scan: no scan state in storage yet - run the research system on the PC once.")
        return {"scan": "no state"}
    if state.get("as_of") >= today:
        return {"scan": "already done"}
    symbols = set(state.get("vol", {})) | set(state.get("ipos", {})) | set(state.get("spikes", {}))
    bars = daily_bars(sorted(symbols))
    fresh = {s: b for s, b in bars.items()
             if datetime.fromtimestamp(b.get("ts", 0) / 1000, IST).date().isoformat() == today}
    if len(fresh) < 50:                         # holiday / data not out: say nothing, keep the state
        return {"scan": f"no session today ({len(fresh)} fresh bars)"}
    msg, ipo_hits, state = scan(state, fresh, today)
    added = []
    for s, close in ipo_hits:
        if not h.positions(user_id, status=h.ACTIVE, symbol=s, bucket=h.WATCHLIST):
            h.db_insert({"user_id": user_id, "stock_name": s, "exchange": "NSE", "entry_price": round(close, 2),
                         "entry_date": today, "status": h.ACTIVE, "bucket": h.WATCHLIST})
            added.append(s)
    save_state(state)
    head = [f"⚡ Evening scan {today} (AWS, from Upstox). Paper only, no orders."]
    body = msg or ["No IPO breakouts, spikes or spike breakouts today."]
    if added:
        body.append(f"Added to 👀 Watchlist: {', '.join(added)}")
    h.send(user_id, "\n".join(head + [""] + body))
    return {"scan": "sent", "ipo": len(ipo_hits), "lines": len(msg)}
