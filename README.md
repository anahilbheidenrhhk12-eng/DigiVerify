# DigiVerify Bot

Telegram reseller workflow for authorized student verification services.

## Features
- Customer menu, balance, history, referrals and support
- $2.50 charge only after confirmed successful verification
- FIFO queue and one-at-a-time provider connection via an authorized Telegram account
- On failure or timeout, customer is not charged
- Admin commands for crediting accounts and reviewing orders
- No fabricated identity documents or false claims of verification

## Railway variables
```
BOT_TOKEN=...
ADMIN_IDS=123456789
TG_API_ID=...
TG_API_HASH=...
TG_SESSION_STRING=...
PROVIDER_USERNAME=gemini_ver_bot
PRICE_CENTS=250
DATABASE_PATH=/data/digiverify.sqlite3
SUPPORT_USERNAME=your_support_username
```

For automatic fulfillment, the separate Telegram account must have access to the provider and you must be permitted to automate and resell its service. A session string is created on your own computer, not through a publicly exposed form.

Deploy: install dependencies from `requirements.txt`, run `python bot.py`, and mount a persistent Railway volume at `/data`.

## Administrator commands
`/credit USER_ID AMOUNT` adds USD credit (e.g. `/credit 12345 10.00`).
`/orders` shows recent orders.
`/balance` shows your own balance.
`/whoami` shows your Telegram numeric user ID.

Customer payments are tracked as manual admin-approved credits, not as automatic real-money collection.
