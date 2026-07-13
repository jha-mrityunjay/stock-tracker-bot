# stock-tracker-bot

Telegram bot to track NSE stocks. Runs on **AWS Lambda** behind a Function URL,
using Telegram webhooks. Prices come from the **Upstox Market Quote API**; entries
are stored in **Supabase**.

## Why this shape

The bot used to long-poll Telegram from an always-on Railway container. That needs a
process running 24/7, which is exactly what Lambda can't do — and it's why the bot
died whenever the container did.

In webhook mode, Telegram POSTs to the Lambda only when someone actually sends a
message. A personal bot is a few hundred invocations a month, against Lambda's
**always-free 1M requests + 400,000 GB-seconds per month** (permanent, not a
12-month trial). So it costs ₹0 indefinitely, and there's no server to fall over.

## Setup

### 1. Upstox Analytics Token

Market quotes need auth, and Upstox's normal OAuth token **expires at 3:30 AM every
day** — useless for an unattended bot. Use an **Analytics Token** instead: read-only,
valid **1 year**, and no static-IP requirement (Lambda's IP changes constantly, so
this matters).

> Upstox → Developer Apps → **Analytics** tab → **Generate Token**

Only one exists per account, and it cannot place orders. Set a calendar reminder to
regenerate it in a year.

### 2. Config

```powershell
Copy-Item .env.example .env   # then fill in the five values
```

### 3. Deploy

Install the [AWS CLI](https://aws.amazon.com/cli/), then `aws configure` with an
access key from your AWS account. Then:

```powershell
.\deploy.ps1
```

This creates the IAM role, packages the zip, creates/updates the Lambda, gives it a
public Function URL, and registers the webhook with Telegram. It is idempotent — run
it again after any code change.

## How it works

| Concern | Approach |
|---|---|
| Dependencies | **None.** Stdlib `urllib` only, so the zip is ~80 KB and there is no build step to break. |
| Symbol → Upstox key | Upstox wants ISINs (`NSE_EQ\|INE002A01018`), not tickers. The 2,381 NSE equities are baked into `nse_equity.json` (77 KB) at deploy time; an unknown symbol triggers a one-off live refresh. |
| Conversation state | Lambda has no memory between calls. `/add` with no argument sends a **`force_reply`** prompt; the user's reply carries that prompt back with it, so the bot knows which flow it is in without any database. |
| Multi-select | `/exit` and `/remove` let you tick several stocks at once. The selection lives in the **message itself** — Telegram returns the inline keyboard on every callback, so the ☐/☑ marks in the button labels *are* the state. No session store, no DB writes while you're picking. |
| Exits vs deletes | `/exit` marks a stock sold (records today's live price + date, keeps it under 📕 Exited). `/remove` deletes permanently, **by row id** — deleting by symbol would destroy the exit history of a stock you had re-entered. |
| Prices | `/portfolio` fetches every stock in **one** batched Upstox call (up to 500 instruments), not one call per stock. |
| Market closed | `last_price` is 0 outside market hours, so the bot falls back to `cp` (previous close). |
| Security | The Function URL is public, so every request must carry `WEBHOOK_SECRET` in Telegram's `X-Telegram-Bot-Api-Secret-Token` header, or it is rejected with a 403. |
| Failures | The handler always returns 200. A non-200 makes Telegram redeliver the same update forever. |

## Maintenance

- `python build_instruments.py` — refresh `nse_equity.json` with new NSE listings, then redeploy.
- Logs: CloudWatch → `/aws/lambda/stock-tracker-bot`.

`bot.py` is the old Railway long-polling version, kept for reference only. It is no
longer deployed and can be deleted.
