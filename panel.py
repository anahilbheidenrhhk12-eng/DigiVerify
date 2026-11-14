"""Secure, installable mobile operations dashboard for DigiVerify.

The dashboard never returns Telegram StringSessions or API secrets to browsers.
Telegram admins obtain a short-lived one-time login code from /panel.
"""
import asyncio
from contextlib import closing
from decimal import Decimal, InvalidOperation
import json
import hashlib
import logging
import os
from pathlib import Path
import re
import secrets
import time

from aiohttp import web
from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

BASE=Path(__file__).resolve().parent
LOG=logging.getLogger("digiverify.panel")
TTL=365*24*3600
CODE_TTL=600
MAX_MIRRORS=10

def ensure_panel_tables(db):
    with closing(db()) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS session_registry(
          account_id INTEGER PRIMARY KEY, label TEXT NOT NULL,
          source TEXT NOT NULL, secret TEXT, enabled INTEGER NOT NULL DEFAULT 1,
          vip INTEGER, free INTEGER, credits INTEGER, points INTEGER,
          observed_at INTEGER, last_seen INTEGER, last_error TEXT,
          low_threshold INTEGER NOT NULL DEFAULT 2, last_alert INTEGER NOT NULL DEFAULT 0,
          topup_recorded_cents INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0)""")
        con.execute("""CREATE TABLE IF NOT EXISTS session_topup_notes(
          id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
          cents INTEGER NOT NULL, note TEXT NOT NULL, by_uid INTEGER NOT NULL,
          created INTEGER NOT NULL)""")
        con.execute("CREATE TABLE IF NOT EXISTS admin_devices(token_hash TEXT PRIMARY KEY,admin_uid INTEGER NOT NULL,csrf TEXT NOT NULL,expires INTEGER NOT NULL,created INTEGER NOT NULL,last_seen INTEGER NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS provider_claims(txhash TEXT PRIMARY KEY,account_id INTEGER,status TEXT,message TEXT,created INTEGER)")
        con.commit()

def _fernet():
    key=os.getenv("PANEL_ENCRYPTION_KEY","").strip()
    if not key:
        raise RuntimeError("PANEL_ENCRYPTION_KEY is required to store added accounts")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError,TypeError) as exc:
        raise RuntimeError("PANEL_ENCRYPTION_KEY must be a Fernet key") from exc

def load_saved_sessions(db):
    ensure_panel_tables(db)
    try: cipher=_fernet()
    except RuntimeError:
        return []
    with closing(db()) as con:
        rows=con.execute("SELECT account_id,label,secret FROM session_registry WHERE source='panel' AND enabled=1 AND secret IS NOT NULL").fetchall()
    out=[]
    for row in rows:
        try:
            value=cipher.decrypt(row["secret"].encode("ascii")).decode("utf-8")
            out.append((value,str(row["label"]),int(row["account_id"])))
        except (InvalidToken,UnicodeError):
            LOG.error("Stored session %s could not be decrypted",row["account_id"])
    return out

def is_disabled(db,account_id):
    ensure_panel_tables(db)
    with closing(db()) as con:
        r=con.execute("SELECT enabled FROM session_registry WHERE account_id=?",(account_id,)).fetchone()
        return bool(r is not None and not r["enabled"])

def parse_credits(text):
    """Parse absolute wallet counters only; ignore top-up change notifications."""
    t=(text or "").replace("\u200b","")
    regex={
      "vip":[
        r"VIP\s+Priority\s+Turns?\s*:\s*(\d+)",
        r"(?:Remaining|Current\s+Balance)\s*:\s*[^\n]*?(\d+)\s*VIP\b",
      ],
      "free":[
        r"Free\s*/\s*Standard\s+Turns?\s*:\s*(\d+)",
        r"(?:Remaining|Current\s+Balance)\s*:\s*[^\n]*?(\d+)\s*Free\b",
      ],
      "points":[
        r"Reward\s+Points?\s*:\s*(\d+)",
        r"Points?\s*:\s*(\d+)\s*(?:pts|points)\b"
      ],
      "credits":[r"Your\s+Balance\s*:\s*(\d+)\s*verifications?\b"]
    }
    data={}
    for key,patterns in regex.items():
        for p in patterns:
            m=re.search(p,t,re.I)
            if m:
                data[key]=int(m.group(1))
                break
    return data

class AdminPanel:
    def __init__(self,app,bot_module):
        self.app=app
        self.botmod=bot_module
        self.codes={}
        self.sessions={}
        self.failed={}
        self.runner=None
        self.site=None
        self.task=None
        self.refresh_at={}
        self.disabled_env={}
        self.phone_logins={}
        self.phone_rate={}
        self.server=web.Application(client_max_size=65536,middlewares=[self._security])
        self._routes()

    def _routes(self):
        self.server.router.add_get("/",self.index)
        self.server.router.add_get("/app",self.index)
        self.server.router.add_get("/manifest.webmanifest",self.manifest)
        self.server.router.add_get("/sw.js",self.worker_script)
        self.server.router.add_get("/icon.svg",self.icon)
        self.server.router.add_get("/icon-192.png",self.icon_png)
        self.server.router.add_get("/icon-512.png",self.icon_png)
        self.server.router.add_get("/health",self.health)
        self.server.router.add_post("/api/login",self.login)
        self.server.router.add_get("/api/state",self.state)
        self.server.router.add_post("/api/logout",self.logout)
        self.server.router.add_post("/api/sessions",self.add_session)
        self.server.router.add_post("/api/phone/start",self.phone_start)
        self.server.router.add_post("/api/phone/verify",self.phone_verify)
        self.server.router.add_post("/api/sessions/{aid}/refresh",self.refresh)
        self.server.router.add_post("/api/sessions/{aid}/threshold",self.threshold)
        self.server.router.add_post("/api/sessions/{aid}/topup-note",self.record_topup)
        self.server.router.add_post("/api/sessions/{aid}/claim",self.claim_payment)
        self.server.router.add_delete("/api/sessions/{aid}",self.remove_session)
        self.server.router.add_post("/api/sessions/{aid}/enable",self.enable_session)
        self.server.router.add_post("/api/customers/credit",self.customer_credit)
        self.server.router.add_post("/api/orders/{oid}/cancel",self.cancel_order)

    @web.middleware
    async def _security(self,req,handler):
        if req.path.startswith("/api/") and req.path!="/api/login":
            token=req.cookies.get("dv_admin")
            digest=hashlib.sha256((token or "").encode()).hexdigest()
            with closing(self.botmod.db()) as con:
                value=con.execute("SELECT admin_uid,csrf,expires FROM admin_devices WHERE token_hash=?",(digest,)).fetchone()
            if not value or value["expires"]<int(time.time()) or not self.botmod.is_admin(value["admin_uid"]):
                return web.json_response({"error":"Sign in again via /panel in Telegram."},status=401)
            req["admin_uid"]=value["admin_uid"]
            req["csrf"]=value["csrf"]
            if req.method not in ("GET","HEAD"):
                supplied=req.headers.get("X-DigiVerify-CSRF","")
                if not secrets.compare_digest(supplied,value["csrf"]):
                    return web.json_response({"error":"Invalid security token"},status=403)
            if req.path=="/api/state":
                with closing(self.botmod.db()) as con:
                    con.execute("UPDATE admin_devices SET last_seen=? WHERE token_hash=?",(int(time.time()),digest))
                    con.commit()
        response=await handler(req)
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["X-Frame-Options"]="DENY"
        response.headers["Referrer-Policy"]="no-referrer"
        response.headers["Cache-Control"]="no-store"
        return response

    def issue_code(self,admin):
        if not self.botmod.is_admin(admin): return None
        code=secrets.token_urlsafe(20)
        self.codes[code]=(admin,time.monotonic()+CODE_TTL)
        return code

    async def index(self,req):
        return web.FileResponse(BASE/"admin.html",headers={"Cache-Control":"no-store"})

    async def manifest(self,req):
        return web.FileResponse(BASE/"manifest.webmanifest",headers={"Content-Type":"application/manifest+json"})

    async def worker_script(self,req):
        return web.FileResponse(BASE/"sw.js",headers={"Content-Type":"application/javascript"})

    async def icon(self,req):
        return web.FileResponse(BASE/"icon.svg",headers={"Content-Type":"image/svg+xml"})

    async def icon_png(self,req):
        filename="icon-192.png" if req.path.endswith("192.png") else "icon-512.png"
        return web.FileResponse(BASE/filename,headers={"Content-Type":"image/png"})

    async def health(self,req):
        return web.json_response({"service":"DigiVerify Admin","status":"ok"})

    async def login(self,req):
        address=req.remote or "unknown"
        now=time.monotonic()
        strikes=[t for t in self.failed.get(address,[]) if now-t<600]
        if len(strikes)>=12:
            return web.json_response({"error":"Too many attempts. Retry later."},status=429)
        try:
            data=await req.json()
            code=str(data.get("code","")).strip()
        except (ValueError,TypeError):
            code=""
        entry=self.codes.pop(code,None)
        if not entry or entry[1]<now or not self.botmod.is_admin(entry[0]):
            strikes.append(now)
            self.failed[address]=strikes
            return web.json_response({"error":"Code invalid or expired. Send /panel to your bot."},status=401)
        token=secrets.token_urlsafe(32)
        csrf=secrets.token_urlsafe(24)
        with closing(self.botmod.db()) as con:
            con.execute("INSERT INTO admin_devices VALUES(?,?,?,?,?,?)",(hashlib.sha256(token.encode()).hexdigest(),entry[0],csrf,int(time.time())+TTL,int(time.time()),int(time.time())))
            con.commit()
        response=web.json_response({"ok":True,"csrf":csrf})
        response.set_cookie("dv_admin",token,max_age=TTL,httponly=True,secure=True,samesite="Strict",path="/")
        return response

    async def logout(self,req):
        digest=hashlib.sha256((req.cookies.get("dv_admin") or "").encode()).hexdigest()
        with closing(self.botmod.db()) as con:
            con.execute("DELETE FROM admin_devices WHERE token_hash=?",(digest,))
            con.commit()
        response=web.json_response({"ok":True})
        response.del_cookie("dv_admin",path="/")
        return response

    async def _json(self,req):
        try:
            data=await req.json()
            if not isinstance(data,dict):
                raise ValueError("Expected a JSON object")
            return data
        except (ValueError,TypeError) as e:
            raise web.HTTPBadRequest(text="Invalid JSON input") from e

    def _pool(self):
        return self.botmod.MIRROR

    def _mirror(self,aid):
        p=self._pool()
        if not p: return None
        return next((m for m in p.mirrors if getattr(m,"account_id",None)==aid),None)

    def _row(self,aid):
        with closing(self.botmod.db()) as con:
            return con.execute("SELECT * FROM session_registry WHERE account_id=?",(aid,)).fetchone()

    def upsert_mirror(self,mirror,label=None,source="env"):
        aid=int(mirror.account_id)
        with closing(self.botmod.db()) as con:
            con.execute("""INSERT INTO session_registry(account_id,label,source,enabled,updated_at)
              VALUES(?,?,?,?,?)
              ON CONFLICT(account_id) DO UPDATE SET
                label=CASE WHEN session_registry.label='' THEN excluded.label ELSE session_registry.label END,
                enabled=1,updated_at=excluded.updated_at""",
                (aid,label or ("Account "+str(aid)),source,1,int(time.time())))
            con.commit()

    def _summary(self,mirror,row):
        aid=int(row["account_id"])
        vip,free,credits=row["vip"],row["free"],row["credits"]
        total=(vip or 0)+(free or 0) if vip is not None or free is not None else credits
        busy=bool(mirror and mirror.active_order is not None)
        online=bool(mirror and getattr(mirror,"healthy",True) and mirror.client.is_connected())
        setup_state=getattr(mirror,"setup_state","ready") if mirror else "unknown"
        status="disabled" if not row["enabled"] else ("setup_failed" if setup_state=="setup_failed" else ("offline" if not online else ("processing" if busy else ("in-use" if mirror.owner else "online"))))
        phone=getattr(mirror,"phone_masked",None) if mirror else None
        threshold=int(row["low_threshold"])
        return {"id":aid,"label":row["label"],"source":row["source"],
                "status":status,"online":online,"phone":phone,
                "setup_state":setup_state,"setup_message":getattr(mirror,"setup_message","") if mirror else "",
                "vip":vip,"free":free,"credits":credits,"points":row["points"],"total":total,
                "threshold":threshold,"low":bool(total is not None and total<=threshold),
                "observed_at":row["observed_at"],"last_seen":row["last_seen"],
                "error":row["last_error"],"active_order":mirror.active_order if mirror else None,
                "customer":mirror.owner if mirror else None,"topup_recorded_cents":row["topup_recorded_cents"]}

    async def state(self,req):
        p=self._pool()
        mirrors={int(m.account_id):m for m in p.mirrors} if p else {}
        with closing(self.botmod.db()) as con:
            rows=con.execute("SELECT * FROM session_registry ORDER BY label,account_id").fetchall()
            orders=con.execute("SELECT id,uid,status,created FROM orders ORDER BY id DESC LIMIT 30").fetchall()
            stats={r["status"]:r["n"] for r in con.execute("SELECT status,COUNT(*) n FROM orders GROUP BY status")}
            payment_stats={r["status"]:{"count":r["n"],"cents":r["cents"]} for r in con.execute("SELECT status,COUNT(*) n,COALESCE(SUM(cents),0) cents FROM customer_topups GROUP BY status")}
            recent_payments=[dict(r) for r in con.execute("SELECT id,uid,cents,status,created,reviewed FROM customer_topups ORDER BY id DESC LIMIT 30")]
        items=[self._summary(mirrors.get(int(r["account_id"])),r) for r in rows]
        queue=[{"position":i+1,"uid":uid,"order":p.queued_orders.get(uid,[None])[0]} for i,uid in enumerate(p.waiting)] if p else []
        token=req.cookies.get("dv_admin")
        return web.json_response({
            "csrf":req["csrf"],
            "admin":req["admin_uid"],
            "target":int(os.getenv("MIRROR_TARGET","5")),
            "max":MAX_MIRRORS,
            "active":len(mirrors),
            "sessions":items,"queue":queue,
            "orders":[dict(r) for r in orders],
            "order_stats":stats,"payment_stats":payment_stats,"recent_payments":recent_payments,"time":int(time.time())
        })

    async def start(self):
        ensure_panel_tables(self.botmod.db)
        self.runner=web.AppRunner(self.server,access_log=None)
        await self.runner.setup()
        port=int(os.getenv("PORT","8080"))
        self.site=web.TCPSite(self.runner,host="0.0.0.0",port=port)
        await self.site.start()
        self.task=asyncio.create_task(self._health_loop())
        LOG.warning("Admin PWA server listening on port %s",port)
        pool=self._pool()
        if pool:
            for mirror in pool.mirrors:
                self.upsert_mirror(mirror,getattr(mirror,"label",None),getattr(mirror,"source","env"))
                mirror.observed_callback=self.observe
            # Obtain a fresh readout from upstream, but only if idle.
            asyncio.create_task(self._initial_refresh())

    async def stop(self):
        for entry in list(self.phone_logins.values()):
            try: await entry["client"].disconnect()
            except Exception: pass
        self.phone_logins.clear()
        if self.task:
            self.task.cancel()
            try: await self.task
            except asyncio.CancelledError: pass
        if self.runner: await self.runner.cleanup()

    async def _initial_refresh(self):
        await asyncio.sleep(2)
        pool=self._pool()
        if not pool: return
        for m in list(pool.mirrors):
            if m.active_order is None and m.owner is None:
                try: await m.client.send_message(m.peer,"/start")
                except Exception:
                    LOG.exception("Initial wallet refresh failed for account %s",m.account_id)
                await asyncio.sleep(1)

    async def _admins(self):
        ids=set(self.botmod.ADMINS)
        with closing(self.botmod.db()) as con:
            ids.update(r["uid"] for r in con.execute("SELECT uid FROM extra_admins"))
        return ids

    async def _notify(self,body):
        for uid in await self._admins():
            try: await self.app.bot.send_message(uid,body)
            except Exception:
                LOG.warning("Cannot notify admin %s",uid)

    async def observe(self,mirror,msg):
        data=parse_credits(msg.raw_text or "")
        now=int(time.time())
        aid=int(mirror.account_id)
        previous=self._row(aid)
        if not previous:
            self.upsert_mirror(mirror,mirror.label,mirror.source)
            previous=self._row(aid)
        fields={"last_seen":now}
        if data:
            fields["observed_at"]=now
            fields.update(data)
        with closing(self.botmod.db()) as con:
            sets=", ".join(k+"=?" for k in fields)
            con.execute("UPDATE session_registry SET "+sets+" WHERE account_id=?",(*fields.values(),aid))
            con.commit()
        if not data:return
        latest=self._row(aid)
        total=(latest["vip"] or 0)+(latest["free"] or 0) if latest["vip"] is not None or latest["free"] is not None else latest["credits"]
        if total is None:return
        threshold=int(latest["low_threshold"])
        if total<=threshold and now-int(latest["last_alert"] or 0)>3600:
            with closing(self.botmod.db()) as con:
                con.execute("UPDATE session_registry SET last_alert=? WHERE account_id=?",(now,aid))
                con.commit()
            await self._notify(f"🚨 DIGIVERIFY: LOW UPSTREAM BALANCE\n🪞 {mirror.label}\n💎 VIP: {latest['vip'] if latest['vip'] is not None else '?'} | 🎁 Free: {latest['free'] if latest['free'] is not None else '?'}\nRemaining total: {total}\nThreshold: {threshold}\nTop up the ORIGINAL provider account before running out.")
        elif total>threshold and previous and previous["last_alert"]:
            with closing(self.botmod.db()) as con:
                con.execute("UPDATE session_registry SET last_alert=0 WHERE account_id=?",(aid,))
                con.commit()

    async def _health_loop(self):
        while True:
            await asyncio.sleep(30)
            now=time.monotonic()
            for ticket,entry in list(self.phone_logins.items()):
                if entry["expires"]<=now:
                    self.phone_logins.pop(ticket,None)
                    try: await entry["client"].disconnect()
                    except Exception: pass
            pool=self._pool()
            if not pool: continue
            for m in list(pool.mirrors):
                try:
                    was_online=getattr(m,"healthy",True) and m.client.is_connected()
                    if not m.client.is_connected():
                        await m.client.connect()
                    if not await m.client.is_user_authorized():
                        raise RuntimeError("Session expired or Telegram authorization revoked")
                    if getattr(m,"setup_state","ready")=="setup_failed":
                        if time.monotonic()-getattr(m,"last_setup_attempt",0)<300:
                            continue
                        m.last_setup_attempt=time.monotonic()
                        from provider_setup import setup_provider_account
                        ready,reason=await setup_provider_account(m.client,self.botmod.PROVIDER)
                        m.setup_state="ready" if ready else "setup_failed"
                        m.setup_message=reason
                        if not ready:raise RuntimeError("SETUP FAILED: "+reason)
                    m.healthy=True
                    row=self._row(m.account_id)
                    if (not was_online) or (row and row["last_error"]):
                        with closing(self.botmod.db()) as con:
                            con.execute("UPDATE session_registry SET last_error=NULL WHERE account_id=?",(m.account_id,))
                            con.commit()
                        await self._notify(f"✅ DigiVerify mirror is online again: {m.label}")
                except Exception as exc:
                    m.healthy=False
                    err=type(exc).__name__+": "+str(exc)[:120]
                    row=self._row(m.account_id)
                    with closing(self.botmod.db()) as con:
                        con.execute("UPDATE session_registry SET last_error=? WHERE account_id=?",(err,m.account_id))
                        con.commit()
                    if row and row["last_error"]!=err:
                        await self._notify(f"🔴 DigiVerify mirror unavailable: {m.label}\n{err}")

    async def _attach_session(self,session,label,source="panel"):
        """Authorize and register a session on the live pool (never expose secrets)."""
        pool=self._pool()
        if not pool: raise RuntimeError("Mirror pool unavailable")
        if len(pool.mirrors)>=MAX_MIRRORS: raise ValueError("Maximum of 10 accounts reached")
        api=os.getenv("TG_API_ID","")
        api_hash=os.getenv("TG_API_HASH","")
        if not api.isdigit() or not api_hash:
            raise RuntimeError("TG_API_ID and TG_API_HASH required")
        cipher=_fernet()
        client=None
        try:
            client=TelegramClient(StringSession(session),int(api),api_hash)
            await client.connect()
            if not await client.is_user_authorized():
                raise ValueError("Telegram session is not authorized")
            account=await client.get_me()
            aid=int(account.id)
            if self._mirror(aid):
                raise ValueError("This Telegram account is already connected")
            from mirror import ProviderMirror
            mirror=ProviderMirror(client,self.botmod.PROVIDER,self.app,
                self.botmod.finish,self.botmod.classify,self.botmod.is_admin,self.botmod.PRICE_LABEL,
                session_ttl=int(os.getenv("MIRROR_SESSION_TTL","300")),
                result_timeout=int(os.getenv("PROVIDER_TIMEOUT_SECONDS","360")))
            mirror.account_id=aid
            mirror.phone_masked=("••••"+str(account.phone)[-4:]) if getattr(account,"phone",None) else None
            mirror.label=label
            mirror.source=source
            from provider_setup import setup_provider_account
            ready,setup_message=await setup_provider_account(client,self.botmod.PROVIDER)
            mirror.setup_state="ready" if ready else "setup_failed"
            mirror.setup_message=setup_message
            mirror.healthy=bool(ready)
            await mirror.start()
            async with pool.lock:
                if len(pool.mirrors)>=MAX_MIRRORS:
                    await mirror.stop()
                    raise ValueError("Maximum account count reached")
                with closing(self.botmod.db()) as con:
                    if source=="panel":
                        encrypted=cipher.encrypt(session.encode("utf-8")).decode("ascii")
                        con.execute("""INSERT INTO session_registry(account_id,label,source,secret,enabled,updated_at)
                              VALUES(?,?,?,?,1,?)
                              ON CONFLICT(account_id) DO UPDATE SET label=excluded.label,
                               source='panel',secret=excluded.secret,enabled=1,updated_at=excluded.updated_at""",
                               (aid,label,"panel",encrypted,int(time.time())))
                    else:
                        con.execute("""INSERT INTO session_registry(account_id,label,source,enabled,updated_at)
                             VALUES(?,?,?,1,?)
                             ON CONFLICT(account_id) DO UPDATE SET enabled=1,updated_at=excluded.updated_at""",
                             (aid,label,"env",int(time.time())))
                    con.commit()
                pool.mirrors.append(mirror)
                if not ready:
                    with closing(self.botmod.db()) as con:
                        con.execute("UPDATE session_registry SET last_error=? WHERE account_id=?",
                                    ("SETUP FAILED: "+setup_message,aid))
                        con.commit()
                mirror.on_terminal=pool.on_finished
                mirror.observed_callback=self.observe
                self.app.bot_data.setdefault("mirror_clients",[]).append(client)
                await pool._drain()
            return aid
        finally:
            if client and client not in self.app.bot_data.get("mirror_clients",[]):
                try: await client.disconnect()
                except Exception: pass

    async def add_session(self,req):
        data=await self._json(req)
        session=str(data.get("session","")).strip()
        label=str(data.get("label","")).strip()[:50] or "Mirror"
        if not 50<len(session)<5000:
            return web.json_response({"error":"Enter a valid Telegram StringSession."},status=400)
        try:
            aid=await self._attach_session(session,label)
            return web.json_response({"ok":True,"id":aid,"label":label})
        except ValueError as exc:
            return web.json_response({"error":str(exc)},status=400)
        except Exception:
            LOG.exception("Unable to register Telegram session")
            return web.json_response({"error":"Could not connect this session. Check login and server settings."},status=502)

    async def phone_start(self,req):
        """Start a Telegram login on the user's own authorized secondary number."""
        uid=req["admin_uid"]
        now=time.monotonic()
        recent=[t for t in self.phone_rate.get(uid,[]) if now-t<1200]
        if len(recent)>=3:
            return web.json_response({"error":"Telegram login limit reached. Retry in 20 minutes."},status=429)
        data=await self._json(req)
        phone=str(data.get("phone","")).strip()
        label=str(data.get("label","")).strip()[:50] or "Phone Mirror"
        if not re.fullmatch(r"\+[1-9]\d{7,14}",phone):
            return web.json_response({"error":"Phone must be in international format: +countrycode..."},status=400)
        pool=self._pool()
        if not pool or len(pool.mirrors)>=MAX_MIRRORS:
            return web.json_response({"error":"No mirror slots available"},status=409)
        api=os.getenv("TG_API_ID","")
        api_hash=os.getenv("TG_API_HASH","")
        if not api.isdigit() or not api_hash:
            return web.json_response({"error":"Telegram API credentials are missing on Railway."},status=503)
        try: _fernet()
        except RuntimeError:
            return web.json_response({"error":"Encryption key not configured."},status=503)
        client=None
        try:
            client=TelegramClient(StringSession(),int(api),api_hash)
            await client.connect()
            code=await client.send_code_request(phone)
            ticket=secrets.token_urlsafe(20)
            self.phone_logins[ticket]={"client":client,"phone":phone,
                     "hash":code.phone_code_hash,"uid":uid,"label":label,
                     "expires":now+600,"attempts":0,"needs_password":False}
            self.phone_rate[uid]=recent+[now]
            return web.json_response({"ok":True,"ticket":ticket,
                "message":"Telegram sent a login code to your Telegram app or SMS."})
        except Exception:
            LOG.warning("Unable to send Telegram sign-in code",exc_info=False)
            if client:
                try: await client.disconnect()
                except Exception: pass
            return web.json_response({"error":"Telegram could not send the sign-in code. Check the phone number and rate limits."},status=502)

    async def phone_verify(self,req):
        data=await self._json(req)
        ticket=str(data.get("ticket","")).strip()
        entry=self.phone_logins.get(ticket)
        if not entry or entry["uid"]!=req["admin_uid"] or entry["expires"]<time.monotonic():
            return web.json_response({"error":"Login session expired. Start again."},status=401)
        entry["attempts"]+=1
        if entry["attempts"]>5:
            self.phone_logins.pop(ticket,None)
            await entry["client"].disconnect()
            return web.json_response({"error":"Too many login attempts."},status=429)
        code=str(data.get("code","")).strip()
        password=str(data.get("password",""))
        client=entry["client"]
        try:
            if entry["needs_password"]:
                if not password:
                    return web.json_response({"needs_password":True,"ticket":ticket})
                await client.sign_in(password=password)
            else:
                if not re.fullmatch(r"\d{4,8}",code):
                    return web.json_response({"error":"Enter the Telegram login code."},status=400)
                try:
                    await client.sign_in(entry["phone"],code,phone_code_hash=entry["hash"])
                except SessionPasswordNeededError:
                    entry["needs_password"]=True
                    if password:
                        await client.sign_in(password=password)
                    else:
                        return web.json_response({"needs_password":True,"ticket":ticket,
                            "message":"This account requires its two-step verification password."})
            if not await client.is_user_authorized():
                return web.json_response({"error":"Telegram login not completed."},status=400)
            string=client.session.save()
            label=entry["label"]
            self.phone_logins.pop(ticket,None)
            await client.disconnect()
            aid=await self._attach_session(string,label)
            return web.json_response({"ok":True,"id":aid,"label":label})
        except ValueError as exc:
            return web.json_response({"error":str(exc)},status=400)
        except Exception:
            LOG.warning("Telegram login code or password was rejected",exc_info=False)
            return web.json_response({"error":"Telegram rejected the login details. Check the code/password."},status=400)

    async def remove_session(self,req):
        try: aid=int(req.match_info["aid"])
        except ValueError: raise web.HTTPBadRequest()
        row=self._row(aid)
        if not row: return web.json_response({"error":"Unknown account"},status=404)
        pool=self._pool()
        mirror=self._mirror(aid)
        if mirror:
            async with pool.lock:
                if mirror.active_order is not None or mirror in pool.owners.values():
                    return web.json_response({"error":"Account is busy. Finish current customer first."},status=409)
                pool.mirrors.remove(mirror)
                if row["source"]=="env":
                    # Keep original environment session in memory for immediate re-enable.
                    self.disabled_env[aid]=mirror.client.session.save()
                await mirror.stop()
                await mirror.client.disconnect()
                if mirror.client in self.app.bot_data.get("mirror_clients",[]):
                    self.app.bot_data["mirror_clients"].remove(mirror.client)
                await pool._drain()
        with closing(self.botmod.db()) as con:
            if row["source"]=="panel":
                con.execute("DELETE FROM session_registry WHERE account_id=?",(aid,))
            else:
                con.execute("UPDATE session_registry SET enabled=0 WHERE account_id=?",(aid,))
            con.commit()
        return web.json_response({"ok":True,"message":"Panel-added session removed; environment session disabled."})

    async def enable_session(self,req):
        try: aid=int(req.match_info["aid"])
        except ValueError: raise web.HTTPBadRequest()
        row=self._row(aid)
        if not row or row["source"]!="env":
            return web.json_response({"error":"Only environment-managed sessions can be reenabled here."},status=400)
        if self._mirror(aid):
            return web.json_response({"error":"Account is already online"},status=409)
        session=self.disabled_env.get(aid)
        if not session:
            return web.json_response({"error":"Railway restart required to retrieve this environment session."},status=409)
        try:
            await self._attach_session(session,row["label"],source="env")
            self.disabled_env.pop(aid,None)
            return web.json_response({"ok":True})
        except Exception:
            LOG.exception("Could not re-enable environment session")
            return web.json_response({"error":"Account could not reconnect."},status=502)

    async def refresh(self,req):
        aid=int(req.match_info["aid"])
        m=self._mirror(aid)
        if not m: return web.json_response({"error":"Account offline"},status=404)
        p=self._pool()
        if m.active_order is not None or m in p.owners.values():
            return web.json_response({"error":"Account busy. Refresh only while idle."},status=409)
        now=time.monotonic()
        if now-self.refresh_at.get(aid,0)<30:
            return web.json_response({"error":"Wait 30 seconds before refreshing again."},status=429)
        try:
            await m.client.send_message(m.peer,"/start")
            self.refresh_at[aid]=now
            return web.json_response({"ok":True,"message":"Requested an updated balance from original bot. Watch for its reply."})
        except Exception:
            LOG.exception("Balance refresh failed")
            return web.json_response({"error":"Original bot did not accept the refresh request."},status=502)

    async def threshold(self,req):
        aid=int(req.match_info["aid"])
        data=await self._json(req)
        try: value=int(data.get("threshold"))
        except (ValueError,TypeError): value=-1
        if not 0<=value<=100:
            return web.json_response({"error":"Threshold must be 0 to 100"},status=400)
        with closing(self.botmod.db()) as con:
            cur=con.execute("UPDATE session_registry SET low_threshold=?,last_alert=0 WHERE account_id=?",(value,aid))
            con.commit()
        if not cur.rowcount: return web.json_response({"error":"Unknown account"},status=404)
        return web.json_response({"ok":True})


    async def claim_payment(self,req):
        aid=int(req.match_info["aid"])
        data=await self._json(req)
        tx=str(data.get("txhash","")).strip().lower()
        if not re.fullmatch(r"0x[a-f0-9]{64}",tx):
            return web.json_response({"error":"Invalid BEP20 TxHash (0x + 64 hex characters)"},status=400)
        m=self._mirror(aid)
        pool=self._pool()
        if not m or not pool or not m.client.is_connected() or m.active_order is not None or m.owner is not None:
            return web.json_response({"error":"Account must be connected and idle"},status=409)
        with closing(self.botmod.db()) as con:
            try:
                con.execute("INSERT INTO provider_claims VALUES(?,?,?,?,?)",(tx,aid,"reserved","Not yet confirmed",int(time.time())))
                con.commit()
            except Exception:
                return web.json_response({"error":"TxHash already registered; cannot submit twice"},status=409)
        try:
            async with m.lock:
                if m.owner is not None or m.active_order is not None:
                    raise RuntimeError("Account became busy")
                m.mute_until=time.monotonic()+130
                async def latest():
                    return await m.client.get_messages(m.peer,limit=12)
                async def button(parts,after):
                    for msg in await latest():
                        if msg.id<=after: continue
                        if msg.out: continue
                        for i,line in enumerate(msg.buttons or []):
                            for j,b in enumerate(line):
                                if any(p in (getattr(b,"text","") or "").lower() for p in parts):
                                    return msg,i,j
                    raise RuntimeError("Expected provider button not found")
                initial=await latest()
                after=max((x.id for x in initial),default=0)
                await m.client.send_message(m.peer,"/start")
                await asyncio.sleep(3)
                msg,i,j=await button(["top up","topup","buy credits"],after)
                after=msg.id
                await msg.click(i,j)
                await asyncio.sleep(3)
                msg,i,j=await button(["binance pay","bep20"],after)
                after=msg.id
                await msg.click(i,j)
                await asyncio.sleep(3)
                msg,i,j=await button(["enter txid","txhash","claim automatically"],after)
                await msg.click(i,j)
                await asyncio.sleep(2)
                previous=await latest()
                last_id=max((x.id for x in previous),default=0)
                await m.client.send_message(m.peer,tx)
                status="pending"
                reply="Provider has not confirmed the transaction yet"
                for _ in range(35):
                    await asyncio.sleep(2)
                    messages=[x for x in await latest() if not x.out and x.id>last_id]
                    body="\n".join(x.raw_text or "" for x in messages)
                    lower=body.lower()
                    if "top-up successful" in lower or "top up successful" in lower:
                        status="success";reply=body[:1500];break
                    if any(x in lower for x in ["already claimed","already used","invalid transaction","transaction not found","top-up failed"]):
                        status="failed";reply=body[:1500];break
                with closing(self.botmod.db()) as con:
                    con.execute("UPDATE provider_claims SET status=?,message=? WHERE txhash=?",(status,reply,tx))
                    con.commit()
                if status=="success":
                    await self._notify("✅ BEP20 top-up confirmed for "+m.label)
                return web.json_response({"ok":True,"status":status,"message":reply})
        except Exception:
            LOG.exception("Upstream claim not confirmed")
            with closing(self.botmod.db()) as con:
                con.execute("UPDATE provider_claims SET status='review',message='Check provider chat manually' WHERE txhash=?",(tx,))
                con.commit()
            return web.json_response({"error":"Not confirmed. Check original bot before proceeding. TxHash reserved against duplicate use.", "status":"review"},status=409)
        finally:
            m.mute_until=time.monotonic()+15

    async def record_topup(self,req):
        aid=int(req.match_info["aid"])
        data=await self._json(req)
        try:
            cents=int((Decimal(str(data.get("amount","0")))*100).to_integral_exact())
            if not 0<cents<=100000000: raise ValueError()
        except (InvalidOperation,ValueError):
            return web.json_response({"error":"Enter a positive amount in USD."},status=400)
        note=str(data.get("note","")).strip()[:240] or "Manual provider payment record"
        with closing(self.botmod.db()) as con:
            con.execute("BEGIN IMMEDIATE")
            cur=con.execute("UPDATE session_registry SET topup_recorded_cents=topup_recorded_cents+? WHERE account_id=?",(cents,aid))
            if not cur.rowcount:
                con.rollback()
                return web.json_response({"error":"Unknown account"},status=404)
            con.execute("INSERT INTO session_topup_notes(account_id,cents,note,by_uid,created) VALUES(?,?,?,?,?)",
                        (aid,cents,note,req["admin_uid"],int(time.time())))
            con.commit()
        return web.json_response({"ok":True,"notice":"Payment recorded locally. No credit is added to the original provider account; check upstream to confirm."})

    async def customer_credit(self,req):
        data=await self._json(req)
        try:
            uid=int(data.get("uid","0"))
            cents=int((Decimal(str(data.get("amount","0")))*100).to_integral_exact())
            if uid<=0 or not 0<cents<=100000000: raise ValueError()
        except (InvalidOperation,ValueError):
            return web.json_response({"error":"Valid customer Telegram ID and USD amount required."},status=400)
        self.botmod.user(uid)
        with closing(self.botmod.db()) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE users SET balance=balance+? WHERE uid=?",(cents,uid))
            con.execute("INSERT INTO ledger(uid,amount,reason,created) VALUES(?,?,?,?,?)",
                        (uid,cents,"Panel credit by "+str(req["admin_uid"]),int(time.time())))
            con.commit()
        return web.json_response({"ok":True,"message":"Customer DigiVerify wallet credited successfully."})

    async def cancel_order(self,req):
        try: oid=int(req.match_info["oid"])
        except ValueError: raise web.HTTPBadRequest()
        with closing(self.botmod.db()) as con:
            con.execute("BEGIN IMMEDIATE")
            row=con.execute("SELECT uid,status FROM orders WHERE id=?",(oid,)).fetchone()
            if not row:
                con.rollback()
                return web.json_response({"error":"Order not found."},status=404)
            if row["status"] not in ("queued","processing"):
                con.rollback()
                return web.json_response({"error":"This order is already completed."},status=409)
            con.execute("UPDATE orders SET status='cancelled',result='Cancelled by panel admin' WHERE id=?",(oid,))
            con.commit()
        pool=self._pool()
        if pool: pool.cancel(oid)
        try: await self.app.bot.send_message(row["uid"],f"🛑 Order #{oid} cancelled by administrator. No wallet deduction.")
        except Exception: pass
        return web.json_response({"ok":True})
