"""Pool of independently authorized Telegram user sessions.

One customer is bound to one upstream account until /done or completion.
Waiting customers are admitted FIFO. Never store Telegram session secrets in chat.
"""
import asyncio
import logging
import time

LOG=logging.getLogger(__name__)

class MirrorPool:
    def __init__(self,mirrors,bot,finished=None,started=None):
        self.mirrors=mirrors
        self.bot=bot
        self.owners={}
        self.waiting=[]
        self.queued_orders={}
        self.lock=asyncio.Lock()
        self.finished=finished
        self.started=started

    def _find(self,uid):
        m=self.owners.get(uid)
        return m if m in self.mirrors else None

    def _available(self):
        assigned=set(self.owners.values())
        for m in self.mirrors:
            if m not in assigned and m.client.is_connected() and m.may_start(-1):
                return m
        return None

    async def _allocate(self,uid):
        m=self._find(uid)
        if m: return m
        m=self._available()
        if not m: return None
        self.owners[uid]=m
        try:
            ok=await m.open(uid)
            if not ok:
                self.owners.pop(uid,None)
                return None
            return m
        except Exception:
            self.owners.pop(uid,None)
            LOG.exception("Cannot initialize upstream mirror")
            return None

    async def open(self,uid):
        async with self.lock:
            m=await self._allocate(uid)
            if m: return True
            if uid not in self.waiting:
                self.waiting.append(uid)
            return False

    def position(self,uid):
        try: return self.waiting.index(uid)+1
        except ValueError: return 0

    async def send_text(self,uid,text):
        m=self._find(uid)
        return await m.send_text(uid,text) if m else False

    async def press(self,uid,token):
        m=self._find(uid)
        if not m: return False,"Your upstream session is not active. Use /start."
        return await m.press(uid,token)

    def may_start(self,uid):
        return self._find(uid) is not None or bool(self._available())

    async def submit_order(self,uid,oid,url):
        async with self.lock:
            m=await self._allocate(uid)
            if m:
                ok=await m.submit_order(uid,oid,url)
                if ok: return "processing"
            self.queued_orders[uid]=(oid,url)
            if uid not in self.waiting:
                self.waiting.append(uid)
            return "queued"

    async def _drain(self):
        # Called with pool lock. Preserve FIFO and skip customers who left.
        while self.waiting:
            m=self._available()
            if not m: return
            uid=self.waiting.pop(0)
            assigned=await self._allocate(uid)
            if not assigned:
                self.waiting.insert(0,uid)
                return
            try:
                if uid in self.queued_orders:
                    oid,url=self.queued_orders.pop(uid)
                    if self.finished and not self.finished(oid):
                        await assigned.release(uid)
                        self.owners.pop(uid,None)
                        continue
                    ok=await assigned.submit_order(uid,oid,url)
                    if not ok:
                        self.queued_orders[uid]=(oid,url)
                        self.waiting.insert(0,uid)
                        await assigned.release(uid)
                        self.owners.pop(uid,None)
                        return
                    if self.started: self.started(oid)
                    await self.bot.send_message(uid,f"📨 Order #{oid}: your turn has arrived. The original bot is processing your link.")
                else:
                    await self.bot.send_message(uid,"✅ Your turn has arrived! The original bot is opening now.")
            except Exception:
                LOG.exception("Failed to start queued session")
                self.owners.pop(uid,None)
                self.waiting.insert(0,uid)
                return

    async def release(self,uid):
        async with self.lock:
            if uid in self.waiting:
                self.waiting.remove(uid)
                self.queued_orders.pop(uid,None)
                return True
            m=self._find(uid)
            if not m: return False
            ok=await m.release(uid)
            if not ok: return False
            self.owners.pop(uid,None)
            await self._drain()
            return True

    def cancel(self,oid):
        for uid,(order_id,url) in list(self.queued_orders.items()):
            if order_id==oid:
                self.queued_orders.pop(uid,None)
                if uid in self.waiting: self.waiting.remove(uid)
                return
        for m in self.mirrors:
            if m.active_order==oid:
                m.cancel(oid)
                return

    async def on_finished(self,mirror,uid):
        # Delay briefly to let the upstream finish sending its final messages.
        await asyncio.sleep(2)
        async with self.lock:
            if self.owners.get(uid) is not mirror or mirror.active_order is not None:
                return
            mirror.lease_until=time.monotonic()
            ok=await mirror.release(uid)
            if ok:
                self.owners.pop(uid,None)
            await self._drain()

    async def run(self):
        while True:
            await asyncio.sleep(3)
            async with self.lock:
                for uid,m in list(self.owners.items()):
                    if m.active_order is None and not m.current_user(uid):
                        m.owner=None
                        self.owners.pop(uid,None)
                await self._drain()

    def status(self):
        busy=sum(m.active_order is not None for m in self.mirrors)
        active=len(self.owners)
        return f"🪞 Mirrors: {len(self.mirrors)}\n⚙️ Active sessions: {active}\n📨 Processing: {busy}\n⏳ Waiting customers: {len(self.waiting)}"
