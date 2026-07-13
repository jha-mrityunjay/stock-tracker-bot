"""Regenerate nse_equity.json (symbol -> Upstox instrument_key) from the Upstox
instrument master. Run this occasionally to pick up new NSE listings; the bot
also refetches on-demand when it sees an unknown symbol.
"""

import gzip
import json
import urllib.request

URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

req = urllib.request.Request(URL, headers={"User-Agent": (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)})
with urllib.request.urlopen(req, timeout=60) as r:
    records = json.loads(gzip.decompress(r.read()))

symbols = {
    d["trading_symbol"]: d["instrument_key"]
    for d in records
    if d.get("segment") == "NSE_EQ" and d.get("instrument_type") == "EQ"
}

with open("nse_equity.json", "w") as f:
    json.dump(symbols, f, separators=(",", ":"), sort_keys=True)

print(f"wrote nse_equity.json with {len(symbols)} NSE equities")
