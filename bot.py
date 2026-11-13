import os, asyncio, sqlite3, time, re, logging
from contextlib import closing
from urllib.parse import urlparse
from decimal import Decimal
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from telethon import TelegramClient
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO)
TOKEN=os.getenv("BOT_TOKEN","")
ADMINS={int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit()}
PATH=os.getenv("DATABASE_PATH","/data/digiverify.sqlite3")
PRICE=int(os.getenv("PRICE_CENTS","250"))
PROVIDER=os.getenv("PROVIDER_USERNAME","gemini_ver_bot").lstrip("@")
SUPPORT=os.getenv("SUPPORT_USERNAME","").lstrip("@")
CLIENT=None
Q=asyncio.Queue()
LOCK=asyncio.Lock()

def db():
    os.makedirs(os.path.dirname(os.path.abspath(PATH)),exist_ok=True)
    c=sqlite3.connect(PATH,timeout=30)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY,balance INTEGER DEFAULT 0,points INTEGER DEFAULT 0,referrer INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER,url TEXT,status TEXT,result TEXT,created INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER,amount INTEGER,reason TEXT,created INTEGER)")
    c.commit()
    return c

def user(uid,ref=None):
    with closing(db()) as c:
        if c.execute("SELECT uid FROM users WHERE uid=?",(uid,)).fetchone(): return
        if ref==uid or not ref or not c.execute("SELECT uid FROM users WHERE uid=?",(ref,)).fetchone(): ref=None
        c.execute("INSERT INTO users(uid,referrer) VALUES (?,?)",(uid,ref))
        if ref: c.execute("UPDATE users SET points=points+1 WHERE uid=?",(ref,))
        c.commit()

def balance(uid):
    with closing(db()) as c:
        r=c.execute("SELECT balance,points FROM users WHERE uid=?",(uid,)).fetchone()
        return (r["balance"],r["points"]) if r else (0,0)

def menu():
    buttons=[
      [InlineKeyboardButton("🎓 Verify Student",callback_data="verify")],
      [InlineKeyboardButton("💰 Wallet / Top Up",callback_data="wallet"),InlineKeyboardButton("📜 History",callback_data="history")],
      [InlineKeyboardButton("💎 VIP Priority",callback_data="vip"),InlineKeyboardButton("🎁 Invite Friends",callback_data="invite")],
      [InlineKeyboardButton("🎟 Promo Code",callback_data="promo"),InlineKeyboardButton("📚 Edu Services",callback_data="services")],
      [InlineKeyboardButton("❓ Help & Guide",callback_data="help"),InlineKeyboardButton("🛟 Support",callback_data="support")]
    ]
    return InlineKeyboardMarkup(buttons)

async def start(u,c):
    uid=u.effective_user.id
    ref=int(c.args[0]) if c.args and c.args[0].isdigit() else None
    user(uid,ref)
    amount,points=balance(uid)
    await u.effective_message.reply_text("👋 WELCOME TO DIGIVERIFY! 🎓\n━━━━━━━━━━━━━━━━━━\n👤 Dear Customer: "+u.effective_user.first_name+"\n💰 Balance: $"+format(amount/100,".2f")+"\n🎁 Reward Points: "+str(points)+"\n💎 Student Verification: $"+format(PRICE/100,".2f")+"\n🛡 Failed verification: NO CHARGE\n━━━━━━━━━━━━━━━━━━",reply_markup=menu())

async def wallet(u,c):
    user(u.effective_user.id)
    a,p=balance(u.effective_user.id)
    await u.effective_message.reply_text("💰 WALLET\nBalance: $"+format(a/100,".2f")+"\n🎁 Reward Points: "+str(p)+"\n\nTo top up, contact support. Only admins can add confirmed payments.")

async def history(u,c):
    with closing(db()) as con:
        rows=con.execute("SELECT id,status FROM orders WHERE uid=? ORDER BY id DESC LIMIT 12",(u.effective_user.id,)).fetchall()
    await u.effective_message.reply_text("📜 HISTORY\n"+("\n".join("#"+str(r["id"])+" — "+r["status"] for r in rows) or "No orders yet."))

async def callback(u,c):
    q=u.callback_query
    await q.answer()
    action=q.data
    if action=="wallet": return await wallet(u,c)
    if action=="history": return await history(u,c)
    if action=="verify": msg="🎓 GOOGLE STUDENT VERIFICATION\nSend your current URL:\nhttps://services.sheerid.com/verify/...\n\nOnly successful verifications are charged."
    elif action=="invite":
        bot=await c.bot.get_me()
        msg="🎁 INVITE FRIENDS\nhttps://t.me/"+bot.username+"?start="+str(q.from_user.id)+"\nEarn 1 point for every new member."
    elif action=="vip": msg="💎 VIP PRIORITY\nPriority plans are not enabled at this time."
    elif action=="promo": msg="🎟 PROMO CODE\nNo active codes."
    elif action=="services": msg="📚 EDU SERVICES\n🎓 Student Verification — $"+format(PRICE/100,".2f")+" on success."
    elif action=="help": msg="❓ HELP\nSend your own valid SheerID verification link and wait for the result. Use authentic student credentials and documents. Refresh the link and retry after a confirmed failure."
    elif action=="support": msg="🛟 SUPPORT\n"+("@"+SUPPORT if SUPPORT else "Contact your administrator.")
    else: msg="Use /start"
    await q.message.reply_text(msg)

def valid(url):
    try:
        u=urlparse(url)
        return u.scheme=="https" and u.hostname=="services.sheerid.com" and u.path.startswith("/verify/") and len(u.path)>8 and len(url)<2000
    except ValueError: return False

async def intake(u,c):
    url=u.message.text.strip()
    if not valid(url):
        await u.message.reply_text("Please send a valid https://services.sheerid.com/verify/... URL.")
        return
    uid=u.effective_user.id
    user(uid)
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        current=con.execute("SELECT balance FROM users WHERE uid=?",(uid,)).fetchone()["balance"]
        pending=con.execute("SELECT 1 FROM orders WHERE uid=? AND status IN ('queued','processing')",(uid,)).fetchone()
        if pending:
            con.rollback(); await u.message.reply_text("You already have an active request."); return
        if current<PRICE:
            con.rollback(); await u.message.reply_text("❌ Insufficient funds. Required: $"+format(PRICE/100,".2f")); return
        cur=con.execute("INSERT INTO orders(uid,url,status,created) VALUES (?,?,?,?)",(uid,url,"queued",int(time.time())))
        oid=cur.lastrowid
        con.commit()
    await Q.put((oid,uid,url))
    await u.message.reply_text("🚀 REQUEST QUEUED\n🆔 Order #"+str(oid)+"\n⏳ Waiting for verification.\n💰 No charge unless confirmed successful.")

def finish(oid,status,detail=""):
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        row=con.execute("SELECT uid,status FROM orders WHERE id=?",(oid,)).fetchone()
        if not row or row["status"] in ("success","failed","review"):
            con.rollback(); return "review"
        if status=="success":
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
                con.execute("UPDATE orders SET status='processing' WHERE id=?",(oid,))
                con.commit()
            await app.bot.send_message(uid,"📩 VERIFICATION REQUEST RECEIVED\n🆔 Order #"+str(oid)+"\n⏳ Processing...")
            if not CLIENT:
                finish(oid,"review","Provider account not configured")
                await app.bot.send_message(uid,"⚠️ Service not connected. No balance deducted.")
                continue
            async with LOCK:
                async with CLIENT.conversation(PROVIDER,timeout=360,exclusive=True) as conv:
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
                        if status=="success": await app.bot.send_message(uid,"🎉 VERIFICATION SUCCESSFUL!\n🔗 Activation Link:\n"+detail+"\n💎 Credit used: $"+format(PRICE/100,".2f"))
                        else: await app.bot.send_message(uid,"⚠️ Result needs admin review. No balance deducted.")
                    elif outcome=="failed":
                        finish(oid,"failed")
                        await app.bot.send_message(uid,"❌ VERIFICATION COULD NOT BE COMPLETED\n🛡 ZERO CREDITS DEDUCTED!\n🔄 Refresh your SheerID link and try again.")
                    else:
                        finish(oid,"review","Result missing or timeout")
                        await app.bot.send_message(uid,"⚠️ Verification result not confirmed. No balance deducted. Contact support before retrying.")
        except Exception:
            logging.exception("Order %s failed",oid)
            finish(oid,"review","Provider exception")
            try: await app.bot.send_message(uid,"⚠️ Processing was interrupted. No charge; contact support.")
            except Exception: pass
        finally: Q.task_done()

async def credit(u,c):
    if u.effective_user.id not in ADMINS: return
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
    if u.effective_user.id not in ADMINS: return
    with closing(db()) as con:
        rows=con.execute("SELECT id,uid,status FROM orders ORDER BY id DESC LIMIT 15").fetchall()
    await u.message.reply_text("📋 ORDERS\n"+("\n".join("#"+str(r["id"])+" "+str(r["uid"])+" "+r["status"] for r in rows) or "None"))

async def whoami(u,c):
    await u.message.reply_text("Your Telegram ID: "+str(u.effective_user.id))

async def init(app):
    global CLIENT
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
        except Exception:
            logging.exception("Unable to connect Telegram account")
            CLIENT=None
    with closing(db()) as con:
        con.execute("UPDATE orders SET status='review',result='Restart; check original provider status' WHERE status IN ('queued','processing')")
        con.commit()
    app.bot_data["worker"]=asyncio.create_task(worker(app))

async def shutdown(app):
    task=app.bot_data.get("worker")
    if task:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
    if CLIENT: await CLIENT.disconnect()

def main():
    if not TOKEN: raise RuntimeError("BOT_TOKEN missing")
    db().close()
    app=Application.builder().token(TOKEN).post_init(init).post_shutdown(shutdown).build()
    for command,handler in [("start",start),("balance",wallet),("history",history),("credit",credit),("orders",orders),("whoami",whoami)]:
        app.add_handler(CommandHandler(command,handler))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,intake))
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__": main()
