import os, asyncio, sqlite3, time, re, logging
from contextlib import closing
from urllib.parse import urlparse
from decimal import Decimal
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from telethon import TelegramClient
from telethon.sessions import StringSession
from mirror import ProviderMirror

logging.basicConfig(level=logging.WARNING)
TOKEN=os.getenv("BOT_TOKEN","")
ADMINS={int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit()}
PATH=os.getenv("DATABASE_PATH","/data/digiverify.sqlite3")
PRICE=int(os.getenv("PRICE_CENTS","250"))
PRICE_LABEL="$"+format(PRICE/100,".2f")
PROVIDER=os.getenv("PROVIDER_USERNAME","gemini_ver_bot").lstrip("@")
SUPPORT=os.getenv("SUPPORT_USERNAME","").lstrip("@")
CLIENT=None
MIRROR=None
Q=asyncio.Queue()
CANCEL_REQUESTS=set()
LOCK=asyncio.Lock()

def db():
    os.makedirs(os.path.dirname(os.path.abspath(PATH)),exist_ok=True)
    c=sqlite3.connect(PATH,timeout=30)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY,balance INTEGER DEFAULT 0,points INTEGER DEFAULT 0,referrer INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER,url TEXT,status TEXT,result TEXT,created INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER,amount INTEGER,reason TEXT,created INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS extra_admins(uid INTEGER PRIMARY KEY,added_by INTEGER,created INTEGER)")
    c.commit()
    return c

def user(uid,ref=None):
    with closing(db()) as c:
        if c.execute("SELECT uid FROM users WHERE uid=?",(uid,)).fetchone(): return
        if ref==uid or not ref or not c.execute("SELECT uid FROM users WHERE uid=?",(ref,)).fetchone(): ref=None
        c.execute("INSERT INTO users(uid,referrer) VALUES (?,?)",(uid,ref))
        if ref: c.execute("UPDATE users SET points=points+3 WHERE uid=?",(ref,))
        c.commit()

def balance(uid):
    with closing(db()) as c:
        r=c.execute("SELECT balance,points FROM users WHERE uid=?",(uid,)).fetchone()
        return (r["balance"],r["points"]) if r else (0,0)

def is_admin(uid):
    if uid in ADMINS: return True
    with closing(db()) as con:
        return con.execute("SELECT 1 FROM extra_admins WHERE uid=?",(uid,)).fetchone() is not None

def guide_text():
    return f"""💬 HELP CENTER & USER GUIDE
━━━━━━━━━━━━━━━━━━
❓ 1. How to obtain a valid SheerID verification link:

• Step 1: Open Google One Student Page:
https://one.google.com/ai-student
• Step 2: Choose the student discount. The page will redirect to SheerID.
• Step 3: Copy the COMPLETE URL from your browser address bar. It must begin with:
https://services.sheerid.com/verify/...
• Step 4: Paste that FULL LINK directly into THIS bot chat. Do not send screenshots or shortened links.

🛡 2. Customer Satisfaction Guarantee:
• Only charged when verification is CONFIRMED SUCCESSFUL.
• Failed or pending attempts: ZERO wallet deduction.
• If verification fails: refresh the link and try again.
• Use only genuine eligible student details and documents.

☎️ 3. Customer Support:
{('@' + SUPPORT) if SUPPORT else 'Contact the administrator to top up or request help.'}

👇 Use the buttons below to view your wallet or the services catalog."""

def catalog_text(uid):
    cash,pts=balance(uid)
    credit="♾ Unlimited admin testing (FREE)" if is_admin(uid) else f"USD {format(cash/100,'.2f')} ({cash//PRICE} available verification(s))"
    return f"""🛍 EDUCATIONAL & VERIFICATION SERVICES CATALOG
━━━━━━━━━━━━━━━━━━
🎓 1. SheerID Student Verification (Google One AI Premium 5TB)

• Status: 🟢 Automated submission to verification service
• Price: {PRICE_LABEL} per SUCCESSFUL verification
• Processing Time: Approx. 30–60 seconds; may take longer
• Satisfaction Guarantee: FAILED or PENDING attempts NEVER DEDUCT CREDITS
• Requirement: A fresh SheerID link for a genuinely eligible student

🎨 2. Custom Edu Packages (Figma, JetBrains, Notion, Autodesk...)
• Status: 🟡 Contact support to check availability
• Pricing: Contact support

━━━━━━━━━━━━━━━━━━
📊 Your Balance: {credit}
✨ Reward Points: {pts}

👉 To start verification: Open https://one.google.com/ai-student, select the student offer, copy the FULL SheerID verification URL (https://services.sheerid.com/verify/...) and PASTE it directly into THIS chat!"""

def verify_text(uid):
    cash,_=balance(uid)
    gate=("♾ ADMIN TEST MODE: unlimited requests, no wallet deductions." if is_admin(uid)
          else ("✅ Your wallet is funded. Send your link now." if cash>=PRICE
                else f"🔒 Wallet insufficient. Top up at least {PRICE_LABEL} BEFORE sending a link. Use 💰 Wallet / Top Up."))
    return f"""🎓 GOOGLE ONE AI STUDENT VERIFICATION
━━━━━━━━━━━━━━━━━━
💎 Service: Google One AI Premium student verification (5TB)
💵 Price: {PRICE_LABEL} on SUCCESS only
🛡 Failed or pending attempts: ZERO CHARGE

📋 HOW TO GET YOUR LINK
1️⃣ Open https://one.google.com/ai-student
2️⃣ Select the student discount and continue to SheerID.
3️⃣ Copy the FULL URL from your browser address bar:
https://services.sheerid.com/verify/...
4️⃣ Paste that link DIRECTLY into this bot chat.
5️⃣ Wait for the result here. If it fails, refresh the link and try again.

{gate}"""

def menu():
    buttons=[
      [InlineKeyboardButton("💰 Top Up / Wallet",callback_data="wallet"),InlineKeyboardButton("🛍 Edu Services",callback_data="services")],
      [InlineKeyboardButton("📋 History & Balance",callback_data="history"),InlineKeyboardButton("🎟 Promo Code",callback_data="promo")],
      [InlineKeyboardButton("👥 Invite Friends (+3 pts)",callback_data="invite"),InlineKeyboardButton("💬 Support",callback_data="support")],
      [InlineKeyboardButton("💬 Help & Guide",callback_data="help"),InlineKeyboardButton("🎓 Verify Student",callback_data="verify")],
      [InlineKeyboardButton("💎 VIP Priority",callback_data="vip")]
    ]
    return InlineKeyboardMarkup(buttons)

def admin_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancel Order",callback_data="admin_cancel_help"),InlineKeyboardButton("👤 Add Admin",callback_data="admin_add_help")],[InlineKeyboardButton("📋 Orders",callback_data="admin_orders")]])

async def start(u,c):
    uid=u.effective_user.id
    ref=int(c.args[0]) if c.args and c.args[0].isdigit() else None
    user(uid,ref)
    if MIRROR is not None:
        if not await MIRROR.open(uid):
            await u.effective_message.reply_text("⏳ The upstream Telegram account is currently handling another customer. Please try /start again shortly.")
            return
        if is_admin(uid):
            await u.effective_message.reply_text("🛠 ADMIN CONTROLS",reply_markup=admin_menu())
        else:
            cash,_=balance(uid)
            await u.effective_message.reply_text(f"💰 DigiVerify wallet: USD {cash/100:.2f}\n💎 Successful verification: {PRICE_LABEL}\n🛡 Failed / pending attempts: no charge.\n🔒 Add funds before sending a verification link.")
        return
    amount,points=balance(uid)
    role=("♾ ADMIN TEST MODE — FREE & UNLIMITED" if is_admin(uid)
          else f"💰 Wallet Balance: USD {format(amount/100,'.2f')}")
    prompt=("👉 Paste a new SheerID link directly into this chat!" if is_admin(uid) or amount>=PRICE
            else "🔒 Top up your wallet before submitting a link. Unfunded customer trials are blocked.")
    await u.effective_message.reply_text(
        f"""👋 WELCOME TO DIGIVERIFY! 🎓
━━━━━━━━━━━━━━━━━━
👤 Dear Customer: {u.effective_user.first_name} (ID: {uid})
{role}
✨ Reward Points: {points} pts
━━━━━━━━━━━━━━━━━━
🎯 Automated Service: SheerID Google One AI Premium (5TB Cloud + Gemini Advanced)
💎 Price: {PRICE_LABEL} per SUCCESSFUL verification
🛡 Failed or pending attempts: ZERO CREDITS DEDUCTED

👉 Quick Start:
• Open https://one.google.com/ai-student
• Choose the student discount; continue to SheerID
• Copy the full URL starting with https://services.sheerid.com/verify/...
• Paste your link DIRECTLY into THIS bot chat!

{prompt}""",reply_markup=menu())
    if is_admin(uid):
        await u.effective_message.reply_text("🛠 ADMIN CONTROLS",reply_markup=admin_menu())

async def wallet(u,c):
    user(u.effective_user.id)
    uid=u.effective_user.id
    amount,points=balance(uid)
    if is_admin(uid):
        msg=f"📋 CUSTOMER ACCOUNT & WALLET SUMMARY\n\n👤 Telegram ID: {uid}\n💎 ADMIN: ♾ UNLIMITED FREE TESTS\n💰 Wallet: USD {format(amount/100,'.2f')} (never charged for admin tests)\n✨ Reward Points: {points}"
    else:
        msg=f"📋 CUSTOMER ACCOUNT & WALLET SUMMARY\n\n👤 Telegram ID: {uid}\n💰 Wallet Balance: USD {format(amount/100,'.2f')}\n🎓 Price per SUCCESS: {PRICE_LABEL}\n🎟 Available verifications: {amount//PRICE}\n✨ Reward Points: {points}\n\n🔒 Customers must top up BEFORE submitting their first link.\n🛡 Failed or pending attempts do not deduct credits."
    msg+="\n\n💳 To top up, contact "+("@"+SUPPORT if SUPPORT else "the administrator")+". An administrator updates your balance after payment confirmation."
    await u.effective_message.reply_text(msg)

async def history(u,c):
    uid=u.effective_user.id
    user(uid)
    cash,points=balance(uid)
    with closing(db()) as con:
        completed=con.execute("SELECT COUNT(*) AS n FROM orders WHERE uid=? AND status='success'",(uid,)).fetchone()["n"]
        failed=con.execute("SELECT COUNT(*) AS n FROM orders WHERE uid=? AND status='failed'",(uid,)).fetchone()["n"]
        pending=con.execute("SELECT COUNT(*) AS n FROM orders WHERE uid=? AND status IN ('queued','processing','review')",(uid,)).fetchone()["n"]
        topups=con.execute("SELECT COALESCE(SUM(amount),0) AS amount FROM ledger WHERE uid=? AND amount>0",(uid,)).fetchone()["amount"]
        rows=con.execute("SELECT id,status FROM orders WHERE uid=? ORDER BY id DESC LIMIT 7",(uid,)).fetchall()
        recent=con.execute("SELECT amount,created FROM ledger WHERE uid=? AND amount>0 ORDER BY id DESC LIMIT 3",(uid,)).fetchall()
    wallet_text="♾ Unlimited admin testing — FREE" if is_admin(uid) else f"USD {format(cash/100,'.2f')} ({cash//PRICE} verifications)"
    msg=f"""📋 CUSTOMER ACCOUNT & WALLET SUMMARY
━━━━━━━━━━━━━━━━━━
👤 Customer: {u.effective_user.first_name}
🆔 Telegram ID: {uid}
💰 Wallet Balance: {wallet_text}
✨ Reward Points: {points} pts
💵 Total Top-up: USD {format(topups/100,'.2f')}
✅ Successful Verifications: {completed}
❌ Failed Attempts (zero charge): {failed}
⏳ Pending / Review: {pending}

💳 Recent Top-ups:
"""
    msg+=("\n".join(f"• +USD {format(r['amount']/100,'.2f')} on {time.strftime('%Y-%m-%d %H:%M',time.gmtime(r['created']))} UTC" for r in recent) if recent else "• No top-ups")
    msg+="\n\n📜 Recent Requests:\n"
    msg+=("\n".join(f"• #{r['id']} — {r['status'].upper()}" for r in rows) if rows else "• No orders yet")
    await u.effective_message.reply_text(msg)

async def callback(u,c):
    q=u.callback_query
    action=q.data or ""
    if action.startswith("up:"):
        if MIRROR is None:
            await q.answer("Provider mirror unavailable",show_alert=True)
            return
        try:
            ok,popup=await MIRROR.press(q.from_user.id,action[3:])
            if ok:
                await q.answer((popup or "Sent to original bot.")[:190])
            else:
                await q.answer(popup[:190],show_alert=True)
        except Exception:
            logging.exception("Upstream button relay failed")
            await q.answer("Original bot did not accept this action.",show_alert=True)
        return
    await q.answer()
    if action.startswith("admin_"):
        if not is_admin(q.from_user.id):
            await q.message.reply_text("⛔ Admin access required."); return
        if action=="admin_cancel_help": msg="🛑 Cancel an order: /cancel ORDER_ID. Queued orders are stopped immediately; active provider requests cannot be recalled after submission."
        elif action=="admin_add_help": msg="👤 Add another admin: /addadmin TELEGRAM_USER_ID. New admins can manage orders and wallet, and test for free."
        elif action=="admin_orders": return await orders(u,c)
        else: msg="Unknown admin action."
        await q.message.reply_text(msg); return
    if action=="wallet": return await wallet(u,c)
    if action=="history": return await history(u,c)
    uid=q.from_user.id
    if action=="verify": msg=verify_text(uid)
    elif action=="invite":
        bot=await c.bot.get_me()
        msg=f"👥 INVITE FRIENDS (+3 pts)\n━━━━━━━━━━━━━━━━━━\nShare your invite link:\nhttps://t.me/{bot.username}?start={uid}\n\n🎁 Earn 3 reward points for each new user who joins through your referral."
    elif action=="vip": msg="💎 VIP PRIORITY\nThe queue currently processes requests in arrival order. Administrators have unlimited wallet-free testing."
    elif action=="promo": msg="🎟 PROMO CODE\nNo active promotional codes at present."
    elif action=="services": msg=catalog_text(uid)
    elif action=="help": msg=guide_text()
    elif action=="support": msg="🛟 CUSTOMER SUPPORT\n"+("@"+SUPPORT if SUPPORT else "Contact the administrator for support and top-up instructions.")
    else: msg="Use /start to open the main menu."
    await q.message.reply_text(msg)

def valid(url):
    try:
        u=urlparse(url)
        return u.scheme=="https" and u.hostname=="services.sheerid.com" and u.path.startswith("/verify/") and len(u.path)>8 and len(url)<2000
    except ValueError: return False

async def intake(u,c):
    url=u.message.text.strip()
    if not valid(url):
        if MIRROR is not None and not url.lower().startswith("https://services.sheerid.com/verify/"):
            try:
                ok=await MIRROR.send_text(u.effective_user.id,url)
            except Exception:
                logging.exception("Cannot forward original bot command")
                ok=False
            if not ok:
                await u.message.reply_text("⏳ Your upstream session is busy or expired. Use /start to reopen the original bot.")
            return
        await u.message.reply_text("Please send a valid https://services.sheerid.com/verify/... URL.")
        return
    uid=u.effective_user.id
    user(uid)
    if MIRROR is not None and not MIRROR.may_start(uid):
        await u.message.reply_text("⏳ The upstream account is busy with another customer. Retry once their session finishes.")
        return
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        current=con.execute("SELECT balance FROM users WHERE uid=?",(uid,)).fetchone()["balance"]
        pending=con.execute("SELECT 1 FROM orders WHERE uid=? AND status IN ('queued','processing')",(uid,)).fetchone()
        if pending:
            con.rollback(); await u.message.reply_text("You already have an active request."); return
        if not is_admin(uid) and current<PRICE:
            con.rollback()
            await u.message.reply_text(f"🔒 WALLET TOP-UP REQUIRED\n\n🎓 Price: {PRICE_LABEL}\n💰 Your balance: USD {format(current/100,'.2f')}\n\nPlease add funds to your wallet BEFORE submitting a verification link. Tap 💰 Wallet / Top Up or contact support.\n🛡 Failed orders do not deduct credits.",reply_markup=menu())
            return
        cur=con.execute("INSERT INTO orders(uid,url,status,created) VALUES (?,?,?,?)",(uid,url,"queued",int(time.time())))
        oid=cur.lastrowid
        con.commit()
    if MIRROR is not None:
        try:
            accepted=await MIRROR.submit_order(uid,oid,url)
        except Exception:
            logging.exception("Upstream request could not be sent")
            accepted=False
        if not accepted:
            finish(oid,"review","Upstream account busy or unavailable, request not submitted")
            await u.message.reply_text("⚠️ The original bot was not available. The request was not submitted, and your wallet was not charged.")
            return
        with closing(db()) as con:
            con.execute("UPDATE orders SET status='processing' WHERE id=? AND status='queued'",(oid,))
            con.commit()
        await u.message.reply_text(f"📨 Request #{oid} sent to the ORIGINAL bot. Its actual messages and buttons will appear here.\n🛡 DigiVerify will charge {PRICE_LABEL} only after confirmed success." if not is_admin(uid) else f"📨 Admin test #{oid} sent to ORIGINAL bot. All original responses will be mirrored. Free admin test.")
        return
    await Q.put((oid,uid,url))
    await u.message.reply_text(f"💎 VERIFICATION REQUEST QUEUED\n━━━━━━━━━━━━━━━━━━\n🆔 Order #{oid}\n⏳ Waiting for processing.\n"+("♾ Admin test: FREE — no wallet deduction." if is_admin(uid) else f"💰 {PRICE_LABEL} charged only on SUCCESS."))

def finish(oid,status,detail=""):
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        row=con.execute("SELECT uid,status FROM orders WHERE id=?",(oid,)).fetchone()
        if not row:
            con.rollback(); return "review"
        if row["status"] in ("success","failed","review","cancelled"):
            con.rollback(); return row["status"]
        if status=="success" and not is_admin(row["uid"]):
            changed=con.execute("UPDATE users SET balance=balance-? WHERE uid=? AND balance>=?",(PRICE,row["uid"],PRICE))
            if not changed.rowcount: status="review"; detail="Insufficient balance at settlement"
            else: con.execute("INSERT INTO ledger(uid,amount,reason,created) VALUES (?,?,?,?)",(row["uid"],-PRICE,"Verification #"+str(oid),int(time.time())))
        con.execute("UPDATE orders SET status=?,result=? WHERE id=?",(status,detail,oid))
        con.commit(); return status

def classify(text):
    low=text.lower()
    links=re.findall(r"https?://[^\s<>]+",text)
    success=bool(re.search(r"verification successful|successfully verified|verification completed",low))
    for link in links:
        link=link.rstrip(".,)]")
        if success and urlparse(link).hostname in ("one.google.com","gemini.google.com"):
            return "success",link
    if re.search(r"verification (failed|could not be completed|unsuccessful)|unable to verify|verification denied",low):
        return "failed",""
    return None,None

async def worker(app):
    while True:
        oid,uid,url=await Q.get()
        try:
            with closing(db()) as con:
                state=con.execute("SELECT status FROM orders WHERE id=?",(oid,)).fetchone()
            if not state or state["status"]!="queued": continue
            with closing(db()) as con:
                con.execute("UPDATE orders SET status='processing' WHERE id=? AND status='queued'",(oid,))
                con.commit()
            await app.bot.send_message(uid,"📩 VERIFICATION REQUEST RECEIVED\n🆔 Order #"+str(oid)+"\n⏳ Processing...")
            if not CLIENT:
                finish(oid,"review","Provider account not configured")
                await app.bot.send_message(uid,"⚠️ Service not connected. No balance deducted.")
                continue
            async with LOCK:
                with closing(db()) as con:
                    state=con.execute("SELECT status FROM orders WHERE id=?",(oid,)).fetchone()
                if not state or state["status"]=="cancelled": continue
                async with CLIENT.conversation(PROVIDER,timeout=360,exclusive=True) as conv:
                    with closing(db()) as con:
                        state=con.execute("SELECT status FROM orders WHERE id=?",(oid,)).fetchone()
                    if not state or state["status"]=="cancelled": continue
                    await conv.send_message(url)
                    outcome=None
                    deadline=time.monotonic()+360
                    while time.monotonic()<deadline:
                        try: response=await asyncio.wait_for(conv.get_response(),timeout=max(1,deadline-time.monotonic()))
                        except asyncio.TimeoutError: break
                        outcome,detail=classify(response.raw_text or "")
                        if outcome: break
                    if outcome=="success":
                        status=finish(oid,"success",detail)
                        if status=="success": await app.bot.send_message(uid,"🎉 VERIFICATION SUCCESSFUL! 🎉\n━━━━━━━━━━━━━━━━━━\n🔗 Google One AI (5TB) Activation Link:\n"+detail+"\n\n✨ Congratulations! Click the link above to activate your student discount.\n"+("♾ Admin test — ZERO wallet deductions." if is_admin(uid) else "💎 Credit used: "+PRICE_LABEL))
                        elif status!="cancelled": await app.bot.send_message(uid,"⚠️ Result needs admin review. No balance deducted.")
                    elif outcome=="failed":
                        status=finish(oid,"failed")
                        if status!="cancelled": await app.bot.send_message(uid,"❌ VERIFICATION COULD NOT BE COMPLETED\n━━━━━━━━━━━━━━━━━━\n🛡 Satisfaction Guarantee: ZERO CREDITS DEDUCTED from your wallet!\n👉 Please REFRESH your SheerID verification link and try again.\nℹ️ Your balance remains unchanged.")
                    else:
                        status=finish(oid,"review","Result missing or timeout")
                        if status!="cancelled": await app.bot.send_message(uid,"⚠️ Verification result not confirmed. No balance deducted. Contact support before retrying.")
        except Exception:
            logging.exception("Order %s failed",oid)
            status=finish(oid,"review","Provider exception")
            try:
                if status!="cancelled": await app.bot.send_message(uid,"⚠️ Processing was interrupted. No charge; contact support.")
            except Exception: pass
        finally: Q.task_done()

async def addadmin(u,c):
    if not is_admin(u.effective_user.id): return
    try:
        uid=int(c.args[0])
        if uid<=0: raise ValueError()
    except (IndexError,ValueError):
        await u.message.reply_text("Usage: /addadmin TELEGRAM_USER_ID"); return
    with closing(db()) as con:
        con.execute("INSERT OR IGNORE INTO extra_admins(uid,added_by,created) VALUES (?,?,?)",(uid,u.effective_user.id,int(time.time())))
        con.commit()
    await u.message.reply_text(f"✅ Admin {uid} added. They can use /start to access admin controls.")

async def admins_command(u,c):
    if not is_admin(u.effective_user.id): return
    with closing(db()) as con:
        ids=[r["uid"] for r in con.execute("SELECT uid FROM extra_admins ORDER BY created")]
    await u.message.reply_text("👤 ADMINS\nPrimary: "+", ".join(map(str,sorted(ADMINS)))+"\nAdditional: "+(", ".join(map(str,ids)) if ids else "None"))

async def cancel_order(u,c,oid):
    if not is_admin(u.effective_user.id): return
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        row=con.execute("SELECT uid,status FROM orders WHERE id=?",(oid,)).fetchone()
        if not row:
            con.rollback(); await u.effective_message.reply_text("❌ Order not found."); return
        if row["status"] not in ("queued","processing"):
            con.rollback(); await u.effective_message.reply_text(f"⚠️ Order #{oid} already {row['status']}; cannot cancel."); return
        con.execute("UPDATE orders SET status='cancelled',result='Cancelled by admin' WHERE id=?",(oid,))
        con.commit()
    if MIRROR is not None: MIRROR.cancel(oid)
    await u.effective_message.reply_text(f"🛑 Order #{oid} cancelled. No wallet deduction. If it was already sent to the provider, the external request cannot be recalled.")
    try: await c.bot.send_message(row["uid"],f"🛑 Order #{oid} cancelled by admin. No wallet deduction.")
    except Exception: logging.exception("Could not notify cancelled order")

async def cancel_command(u,c):
    if not is_admin(u.effective_user.id): return
    try:
        oid=int(c.args[0])
        if oid<=0: raise ValueError()
    except (IndexError,ValueError):
        await u.message.reply_text("Usage: /cancel ORDER_ID"); return
    await cancel_order(u,c,oid)

async def credit(u,c):
    if not is_admin(u.effective_user.id): return
    try:
        uid=int(c.args[0]); cents=int(Decimal(c.args[1])*100)
        if cents<=0 or cents>100000000: raise ValueError()
    except (IndexError,ValueError):
        await u.message.reply_text("Usage: /credit USER_ID AMOUNT"); return
    user(uid)
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE users SET balance=balance+? WHERE uid=?",(cents,uid))
        con.execute("INSERT INTO ledger(uid,amount,reason,created) VALUES (?,?,?,?)",(uid,cents,"Admin credit",int(time.time())))
        con.commit()
    await u.message.reply_text("✅ Added $"+format(cents/100,".2f")+" to "+str(uid))

async def orders(u,c):
    if not is_admin(u.effective_user.id): return
    with closing(db()) as con:
        rows=con.execute("SELECT id,uid,status FROM orders ORDER BY id DESC LIMIT 15").fetchall()
    await u.effective_message.reply_text("📋 ORDERS\n"+("\n".join("#"+str(r["id"])+" "+str(r["uid"])+" "+r["status"] for r in rows) or "None")+"\n\nTo cancel: /cancel ORDER_ID",reply_markup=admin_menu())

async def services_command(u,c):
    user(u.effective_user.id)
    await u.effective_message.reply_text(catalog_text(u.effective_user.id))

async def guide_command(u,c):
    await u.effective_message.reply_text(guide_text())

async def verify_command(u,c):
    user(u.effective_user.id)
    await u.effective_message.reply_text(verify_text(u.effective_user.id))

async def whoami(u,c):
    await u.message.reply_text("Your Telegram ID: "+str(u.effective_user.id))

async def init(app):
    global CLIENT,MIRROR
    try:
        await app.bot.set_my_description(
            "🎓 DigiVerify — Google One AI Student Verification.\n"
            "💎 Price: $2.50 only after confirmed SUCCESS.\n"
            "🛡 No wallet charge on failed/pending results.\n"
            "🔗 Open https://one.google.com/ai-student, copy your complete "
            "https://services.sheerid.com/verify/... URL and paste it in this bot."
        )
        await app.bot.set_my_short_description(
            "🎓 Student verification • $2.50 on success • no charge on failure."
        )
        await app.bot.set_my_commands([
            ("start","Open main menu"),
            ("services","Product description and catalog"),
            ("guide","How to get and submit a SheerID link"),
            ("verify","Start a verification request"),
            ("balance","Wallet balance and top-up"),
            ("history","Your recent verifications"),
            ("whoami","Your Telegram numeric ID"),
            ("cancel","Admin: cancel a queued order"),
            ("addadmin","Admin: grant admin access"),
            ("admins","Admin: list administrators"),
        ])
    except Exception:
        logging.exception("Unable to update Telegram bot profile")

    session=os.getenv("TG_SESSION_STRING","")
    api=os.getenv("TG_API_ID","")
    hash=os.getenv("TG_API_HASH","")
    if session and api.isdigit() and hash:
        try:
            CLIENT=TelegramClient(StringSession(session),int(api),hash)
            await CLIENT.connect()
            if not await CLIENT.is_user_authorized():
                await CLIENT.disconnect(); CLIENT=None
                logging.error("Telegram session not authorized")
            else:
                mirror=ProviderMirror(
                    CLIENT,PROVIDER,app,finish,classify,is_admin,PRICE_LABEL,
                    session_ttl=int(os.getenv("MIRROR_SESSION_TTL","300")),
                    result_timeout=int(os.getenv("PROVIDER_TIMEOUT_SECONDS","360"))
                )
                await mirror.start()
                MIRROR=mirror
        except Exception:
            logging.exception("Unable to connect Telegram account")
            CLIENT=None
    with closing(db()) as con:
        con.execute("UPDATE orders SET status='review',result='Restart; check original provider status' WHERE status IN ('queued','processing')")
        con.commit()
    if MIRROR is None:
        logging.warning("Mirror not available. Falling back to legacy upstream request workflow.")
        app.bot_data["worker"]=asyncio.create_task(worker(app))

async def shutdown(app):
    task=app.bot_data.get("worker")
    if task:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
    if MIRROR: await MIRROR.stop()
    if CLIENT: await CLIENT.disconnect()

def main():
    if not TOKEN: raise RuntimeError("BOT_TOKEN missing")
    db().close()
    app=Application.builder().token(TOKEN).post_init(init).post_shutdown(shutdown).build()
    for command,handler in [("start",start),("balance",wallet),("history",history),("services",services_command),("guide",guide_command),("verify",verify_command),("credit",credit),("orders",orders),("cancel",cancel_command),("addadmin",addadmin),("admins",admins_command),("whoami",whoami)]:
        app.add_handler(CommandHandler(command,handler))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,intake))
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__": main()
