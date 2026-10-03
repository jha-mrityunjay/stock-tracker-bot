"""Regenerate the baked instrument maps from the Upstox instrument master.

  nse_equity.json      symbol      -> instrument_key   (2.4k NSE equities, ~77 KB)
  nse_underlyings.json underlying  -> underlying_key    (215 F&O underlyings, ~7 KB)

Option *contracts* are deliberately NOT baked: there are 36k of them and the
expiries roll every week, so they're fetched live from the Upstox option-contract
API instead. Underlyings and equities barely change, so baking them keeps cold
starts fast. Re-run this occasionally to pick up new listings, then redeploy.
"""

import gzip
import json
import urllib.request

URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

with urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent": UA}), timeout=60) as r:
    records = json.loads(gzip.decompress(r.read()))

# Tradeable equity series. Filtering on "EQ" alone silently dropped ~900 real
# stocks: HILINFRA, for one, is listed BE (trade-to-trade), so /add just said
# "couldn't get a price" with no hint why. The NSE_EQ segment also carries bonds,
# T-bills, SGBs and government securities (749RJ35, 182D180926, SGBMAR30X) which
# nobody is tracking here, so this is a whitelist rather than a blanket include.
#   EQ  regular          BE  trade-to-trade     BZ  surveillance
#   SM  SME board        ST  SME trade-to-trade
#   IV  InvIT            RR  REIT
EQUITY_SERIES = {"EQ", "BE", "BZ", "SM", "ST", "IV", "RR"}

equities = {
    d["trading_symbol"]: d["instrument_key"]
    for d in records
    if d.get("segment") == "NSE_EQ" and d.get("instrument_type") in EQUITY_SERIES
}

# NIFTY -> NSE_INDEX|Nifty 50, RELIANCE -> NSE_EQ|INE002A01018, ...
# The option-chain API is keyed by the underlying, not the option, so we need this.
underlyings = {
    d["underlying_symbol"]: d["underlying_key"]
    for d in records
    if d.get("segment") == "NSE_FO" and d.get("instrument_type") in ("CE", "PE")
}

for name, data in [("nse_equity.json", equities), ("nse_underlyings.json", underlyings)]:
    with open(name, "w") as f:
        json.dump(data, f, separators=(",", ":"), sort_keys=True)
    print(f"wrote {name}: {len(data)} entries")
