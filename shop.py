import os,re,sqlite3,time,logging
from contextlib import closing
from telegram import InlineKeyboardMarkup,InlineKeyboardButton

class CustomerShop:
    def __init__(self,db,balance,is_admin,admins,price,support=""):
        self.db=db;self.balance=balance;self.is_admin=is_admin;self.admins=admins
        self.price=price;self.support_default=support

    def setup(self):
        with closing(self.db()) as c:
            c.execute("CREATE TABLE IF NOT EXISTS shop_settings(k TEXT PRIMARY KEY,v TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS customer_payment_intents(uid INTEGER PRIMARY KEY,cents INTEGER NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS customer_topups(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER NOT NULL,cents INTEGER NOT NULL,txhash TEXT NOT NULL UNIQUE,status TEXT NOT NULL DEFAULT 'pending',created INTEGER NOT NULL,reviewed INTEGER)")
            c.commit()

    def get(self,key,default=""):
        self.setup()
        with closing(self.db()) as c:
            r=c.execute("SELECT v FROM shop_settings WHERE k=?",(key,)).fetchone()
        return r["v"] if r else default

    def address(self): return self.get("customer_address",os.getenv("CUSTOMER_BEP20_ADDRESS","")).strip()
    def group(self): return self.get("support_group",os.getenv("SUPPORT_GROUP_URL","")).strip()
    def support_name(self): return self.get("support_username",self.support_default).strip().lstrip("@")

    def support_keyboard(self):
        rows=[]
        if self.group(): rows.append([InlineKeyboardButton("👥 DigiVerify Support Group",url=self.group())])
        if self.support_name(): rows.append([InlineKeyboardButton("💬 DigiVerify Admin",url="https://t.me/"+self.support_name())])
        rows.append([InlineKeyboardButton("📩 Request Support",callback_data="support_request")])
        rows.append([InlineKeyboardButton("🏠 Main Menu",callback_data="home")])
        return InlineKeyboardMarkup(rows)

    def payment_keyboard(self):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("USD 2.50",callback_data="buy:250"),InlineKeyboardButton("USD 5.00",callback_data="buy:500")],
            [InlineKeyboardButton("USD 10.00",callback_data="buy:1000"),InlineKeyboardButton("USD 25.00",callback_data="buy:2500")],
            [InlineKeyboardButton("💬 Request Manual Top Up",callback_data="support_request"),InlineKeyboardButton("🏠 Main Menu",callback_data="home")]
        ])

    async def topup(self,u,ctx):
        uid=u.effective_user.id
        cash,_=self.balance(uid)
        method=("💳 Send USDT on BNB Smart Chain (BEP20) to your DIGIVERIFY wallet destination shown after choosing an amount.\nSend the transaction hash to this bot after payment."
                if self.address() else
                "💬 DigiVerify's own payment address is not configured yet. Tap Request Manual Top Up to contact our admin.")
        await u.effective_message.reply_text(
            "💰 DIGIVERIFY — TOP UP / BUY CREDITS\n━━━━━━━━━━━━━━━━━━\n"
            f"👤 Customer: {uid}\n💵 Your DigiVerify Wallet: USD {cash/100:.2f}\n"
            f"🎓 Successful verification: USD {self.price/100:.2f}\n\n{method}\n"
            "🛡 Failed or pending verifications are free. Never pay the original bot.",
            reply_markup=self.payment_keyboard())

    async def choose_amount(self,u,ctx,cents):
        if cents not in (250,500,1000,2500):
            await u.effective_message.reply_text("Choose a valid DigiVerify top-up amount.");return
        address=self.address()
        if not address:
            await u.effective_message.reply_text("💬 Customer payment address is not configured. Request a manual DigiVerify top up.",reply_markup=self.support_keyboard())
            return
        uid=u.effective_user.id
        with closing(self.db()) as c:
            c.execute("INSERT OR REPLACE INTO customer_payment_intents(uid,cents) VALUES(?,?)",(uid,cents))
            c.commit()
        await u.effective_message.reply_text(
            "💰 DIGIVERIFY USDT PAYMENT\n━━━━━━━━━━━━━━━━━━\n"
            f"Amount: {cents/100:.2f} USDT\n"
            f"Network: BNB Smart Chain (BEP20)\nDigiVerify address:\n{address}\n\n"
            "1️⃣ Send the transfer to this DigiVerify wallet.\n"
            "2️⃣ Copy the completed transaction hash (0x + 64 hexadecimal digits).\n"
            "3️⃣ Send the TxHash directly HERE.\n"
            "4️⃣ Admin verifies receipt before your DigiVerify balance is credited.\n"
            "⚠️ This is not the original provider wallet.")

    async def tell_admins(self,ctx,text):
        for uid in self.admins():
            try: await ctx.bot.send_message(chat_id=uid,text=text)
            except Exception: logging.warning("Could not notify DigiVerify admin %s",uid)

    async def request_support(self,u,ctx):
        uid=u.effective_user.id
        await self.tell_admins(ctx,f"🛟 DIGIVERIFY SUPPORT REQUEST\nCustomer: {u.effective_user.first_name}\nTelegram ID: {uid}\nContact customer about wallet or support.")
        await u.effective_message.reply_text("✅ Your message has been sent to DigiVerify administrators.",reply_markup=self.support_keyboard())

    async def support(self,u,ctx):
        text="💬 DIGIVERIFY SUPPORT & COMMUNITY\n━━━━━━━━━━━━━━━━━━\n"
        text+=("👥 Our Support Group: "+self.group()+"\n" if self.group() else "👥 Our group link has not been set yet.\n")
        text+=("👤 DigiVerify Admin: @"+self.support_name() if self.support_name() else "👤 Contact DigiVerify admin via Request Support.")
        await u.effective_message.reply_text(text,reply_markup=self.support_keyboard(),disable_web_page_preview=True)

    async def submit_tx(self,u,ctx,tx):
        tx=tx.strip().lower()
        if not re.fullmatch(r"0x[a-f0-9]{64}",tx):return False
        self.setup()
        uid=u.effective_user.id
        with closing(self.db()) as c:
            row=c.execute("SELECT cents FROM customer_payment_intents WHERE uid=?",(uid,)).fetchone()
        if not row:
            await u.effective_message.reply_text("Choose a DigiVerify top-up amount before submitting your transaction hash.",reply_markup=self.payment_keyboard())
            return True
        cents=int(row["cents"])
        with closing(self.db()) as c:
            try:
                c.execute("BEGIN IMMEDIATE")
                cursor=c.execute("INSERT INTO customer_topups(uid,cents,txhash,status,created) VALUES(?,?,?,'pending',?)",(uid,cents,tx,int(time.time())))
                c.execute("DELETE FROM customer_payment_intents WHERE uid=?",(uid,))
                c.commit()
            except sqlite3.IntegrityError:
                c.rollback()
                await u.effective_message.reply_text("⚠️ This payment TxHash was already submitted.")
                return True
        rid=cursor.lastrowid
        await u.effective_message.reply_text(f"📨 DigiVerify top-up #{rid} is pending. Amount: USD {cents/100:.2f}. Your wallet remains unchanged until confirmation.")
        await self.tell_admins(ctx,f"💰 DIGIVERIFY TOP-UP #{rid}\nCustomer ID: {uid}\nAmount: {cents/100:.2f} USDT\nTxHash: {tx}\nConfirm RECEIPT to YOUR wallet before /approve {rid}. Otherwise /reject {rid}.")
        return True

    async def review(self,u,ctx,approve):
        if not self.is_admin(u.effective_user.id):return
        self.setup()
        try:
            rid=int(ctx.args[0])
            if rid<=0:raise ValueError()
        except (ValueError,IndexError):
            await u.effective_message.reply_text("Usage: /approve REQUEST_ID or /reject REQUEST_ID");return
        with closing(self.db()) as c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT uid,cents,status FROM customer_topups WHERE id=?",(rid,)).fetchone()
            if not row or row["status"]!="pending":
                c.rollback()
                await u.effective_message.reply_text("⚠️ Request not found or already reviewed.");return
            status="approved" if approve else "rejected"
            c.execute("UPDATE customer_topups SET status=?,reviewed=? WHERE id=?",(status,int(time.time()),rid))
            if approve:
                c.execute("UPDATE users SET balance=balance+? WHERE uid=?",(row["cents"],row["uid"]))
                c.execute("INSERT INTO ledger(uid,amount,reason,created) VALUES(?,?,?,?)",(row["uid"],row["cents"],"DigiVerify top-up #"+str(rid),int(time.time())))
            c.commit()
        await u.effective_message.reply_text(f"✅ DigiVerify payment #{rid}: {status}.")
        try:
            await ctx.bot.send_message(chat_id=row["uid"],text=
                f"💰 DigiVerify payment #{rid}: {status.upper()}.\n"+
                (f"✅ USD {row['cents']/100:.2f} added to your wallet." if approve else "Contact DigiVerify support."))
        except Exception:pass

    async def pending(self,u,ctx):
        if not self.is_admin(u.effective_user.id):return
        self.setup()
        with closing(self.db()) as c:
            rows=c.execute("SELECT id,uid,cents,status FROM customer_topups ORDER BY id DESC LIMIT 15").fetchall()
        msg="\n".join(f"#{r['id']} Customer {r['uid']} · USD {r['cents']/100:.2f} · {r['status']}" for r in rows) or "No requests."
        await u.effective_message.reply_text("💰 DIGIVERIFY CUSTOMER TOP-UPS\n"+msg+"\n\n/approve ID only AFTER verifying receipt.")

    async def configure(self,u,ctx,key):
        if not self.is_admin(u.effective_user.id):return
        self.setup()
        val=" ".join(ctx.args).strip()
        validators={
           "payment":lambda x:re.fullmatch(r"0x[a-fA-F0-9]{40}",x) is not None,
           "group":lambda x:re.fullmatch(r"https://t\.me/[A-Za-z0-9_+/-]+",x) is not None,
           "support":lambda x:re.fullmatch(r"@?[A-Za-z0-9_]{5,32}",x) is not None
        }
        keys={"payment":"customer_address","group":"support_group","support":"support_username"}
        if not val or (val.lower()!="off" and not validators[key](val)):
            await u.effective_message.reply_text("Usage: /set"+key+" VALUE (or /set"+key+" off). Set YOUR DigiVerify details, not the original provider's.")
            return
        with closing(self.db()) as c:
            c.execute("INSERT OR REPLACE INTO shop_settings(k,v) VALUES(?,?)",(keys[key],"" if val.lower()=="off" else val))
            c.commit()
        await u.effective_message.reply_text("✅ DigiVerify payment/support settings saved.")
