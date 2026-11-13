# DigiVerify — Telegram Student Verification Reseller Bot

Customer bot: [@DigitopVerifyBot](https://t.me/DigitopVerifyBot)  
Source verification bot: @gemini_ver_bot (via an authorized separate Telegram account)  
Hosted on Railway; persisted in SQLite on a Railway volume at /data.

## Customer experience

1. Start the Telegram bot with \`/start\`.
2. Tap **Edu Services** or use \`/services\` to see the product catalog and price.
3. Tap **Help & Guide** or use \`/guide\`:
   - Open https://one.google.com/ai-student.
   - Choose the student discount and continue to SheerID.
   - Copy the complete browser URL beginning \`https://services.sheerid.com/verify/...\`.
   - Paste that **full link directly into the bot chat**.
4. The bot checks the wallet before it accepts any request.
5. A successful verification costs **$2.50**; failed or inconclusive attempts do not deduct funds. The original provider decides verification outcomes.
6. On a confirmed failure, the customer is told to refresh their link and try again.

**Only original, eligible student information and documents may be used.** This bot does not independently determine student eligibility.

## Wallet policy

- **Administrator**: Unlimited test submissions to DigiVerify without a wallet balance; no wallet charge on success. Admin status comes strictly from the numeric Telegram user ID in \`ADMIN_IDS\`. This does **not** waive any fees or limits imposed by the upstream provider.
- **Ordinary user**: Must have **at least $2.50 in the wallet before submission**. Pressing Verify or sending a link does not bypass the balance check.
- **Success**: Deduct $2.50 once, never for admin.
- **Failure/pending/review**: Deduct $0. No automatic refunds are necessary because charging occurs only on confirmed success.
- **Top-up**: Contact support and pay through an arrangement with the administrator, who uses \`/credit USER_ID AMOUNT\` to credit the wallet. **This version does not automatically collect Binance Pay/USDT or other payments.**

## Admin commands

- \`/whoami\` — discover your numeric Telegram user ID.
- \`/credit USER_ID AMOUNT\` — add USD to a customer's wallet (example: \`/credit 123456789 10.00\`).
- \`/orders\` — see recent orders.
- \`/balance\` — your wallet and admin test status.
- \`/services\`, \`/guide\`, \`/verify\` — see customer-facing descriptions.

For admin privileges, set \`ADMIN_IDS\` on Railway to your own numeric Telegram user ID (comma-separated for multiple admins), **not the bot's ID or username**. Use \`/start\` to confirm the phrase **ADMIN TEST MODE — FREE & UNLIMITED** appears.

## Railway variables

\`\`\`env
BOT_TOKEN=                    # from BotFather, secret
ADMIN_IDS=123456789           # numeric Telegram user ID(s)
TG_API_ID=                    # from my.telegram.org, secret
TG_API_HASH=                  # from my.telegram.org, secret
TG_SESSION_STRING=            # generated locally by setup_session.py, secret
PROVIDER_USERNAME=gemini_ver_bot
PRICE_CENTS=250
DATABASE_PATH=/data/digiverify.sqlite3
SUPPORT_USERNAME=             # optional, without the @
\`\`\`

Never commit secrets to GitHub. The \`setup_session.py\` utility creates a user session on your own PC; keep it secret and add it to Railway Variables.

This app requires a persistent Railway volume mounted at \`/data\`. Deploy from GitHub \`main\`, Dockerfile build, run \`python bot.py\`.

## Implementation limits

- Requests are serialized through the provider account to avoid mixing responses.
- Only explicit successful verification accompanied by an official Google One activation URL is treated as success.
- Provider replies and real enrollment must be checked end-to-end; screenshots alone cannot guarantee the upstream bot's current behavior.
- Interacting with the upstream bot is subject to that service's authorization and resale rules.

## Additional administrator controls

- `/addadmin TELEGRAM_USER_ID`: add another administrator permanently in SQLite (same operational privileges, including free testing and wallet credit). Existing administrators can use this command.
- `/admins`: list primary and additional administrators.
- `/orders`: list order IDs.
- `/cancel ORDER_ID`: cancel a queued or processing order, without charging the customer. Requests already forwarded to the external provider cannot be recalled, but their result is ignored locally and no charge is applied.
- `/start`: shows the administrator control buttons **Cancel Order**, **Add Admin**, and **Orders** for administrators only.

The original administrators in Railway `ADMIN_IDS` remain recognized. Additional administrators are stored in the persistent database at `/data`.


## Multiple Telegram accounts and FIFO queue

Add each **different, authorized Telegram user account** session string to the Railway **secret** variable \`TG_SESSION_STRINGS\`. Format it as a JSON array:

\`\`\`json
["SESSION_STRING_FOR_ACCOUNT_2", "SESSION_STRING_FOR_ACCOUNT_3"]
\`\`\`

Keep the existing \`TG_SESSION_STRING\` for the original account. It is included automatically; additional accounts are loaded from \`TG_SESSION_STRINGS\`. All accounts use the same \`TG_API_ID\` and \`TG_API_HASH\` (your Telegram application credentials), and each must be able to chat with \`@gemini_ver_bot\`. Duplicate Telegram accounts are ignored.

Each account operates one independent upstream conversation at a time. The pool assigns an available account to each customer. When all accounts are busy, customers join a first-in-first-out queue; the bot notifies them when a session becomes available. An order is charged only on confirmed success. On completion or timeout the session is released and the next customer starts. A customer can release an idle session with \`/done\`. Administrators can inspect counts using \`/sessions\`.

**Important:** Add session strings only in Railway Variables, never in Telegram chat or GitHub. Session strings grant account access. Restarting the service marks in-progress orders for review; a production-grade durable queue would require recovery across restarts.
