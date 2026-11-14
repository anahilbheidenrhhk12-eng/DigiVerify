"""Relay Telegram bot messages and button presses through one authorized user account.

One source account can be in only one conversation state at a time.
This module leases that account to a single customer per session.
"""
import asyncio
import io
import logging
import os
import secrets
import re
import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InputFile,
)
from telethon import events, types

LOG = logging.getLogger(__name__)


class ProviderMirror:
    def __init__(self, client, provider, app, finish_order, classify, is_admin,
                 price_label, session_ttl=300, result_timeout=360):
        self.client = client
        self.provider_name = provider.lstrip("@")
        self.app = app
        self.finish = finish_order
        self.classify = classify
        self.is_admin = is_admin
        self.price_label = price_label
        self.session_ttl = max(60, int(session_ttl))
        self.result_timeout = max(60, int(result_timeout))
        self.peer = None
        self.owner = None
        self.lease_until = 0
        self.active_order = None
        self.order_owner = None
        self.timeout_task = None
        self.lock = asyncio.Lock()
        self.callbacks = {}
        self.sent = {}
        self.mute_until = 0
        self.on_terminal = None
        self.observed_callback = None
        self.needs_rotation = False

    async def start(self):
        self.peer = await self.client.get_entity(self.provider_name)
        self.client.add_event_handler(self._new_message, events.NewMessage(chats=self.peer))
        self.client.add_event_handler(self._edited_message, events.MessageEdited(chats=self.peer))
        LOG.warning("Upstream mirror enabled for @%s", self.provider_name)

    async def stop(self):
        self.client.remove_event_handler(self._new_message)
        self.client.remove_event_handler(self._edited_message)
        if self.timeout_task:
            self.timeout_task.cancel()

    def _busy(self, uid):
        if self.owner is None or self.owner == uid:
            return False
        return self.active_order is not None or time.monotonic() < self.lease_until

    def may_start(self, uid):
        return not self._busy(uid) and time.monotonic() >= self.mute_until

    def current_user(self, uid):
        return self.owner == uid and time.monotonic() < self.lease_until

    async def _claim(self, uid, reset=False):
        if not self.may_start(uid):
            return False
        if self.owner != uid or reset:
            self.owner = uid
            self.needs_rotation = False
            self.callbacks.clear()
            self.sent.clear()
        self.lease_until = time.monotonic() + self.session_ttl
        if reset:
            await self.client.send_message(self.peer, "/start")
        return True

    async def open(self, uid):
        async with self.lock:
            return await self._claim(uid, reset=True)

    async def release(self, uid):
        async with self.lock:
            if self.owner != uid or self.active_order is not None:
                return False
            self.owner = None
            self.lease_until = 0
            self.callbacks.clear()
            self.sent.clear()
            return True

    async def send_text(self, uid, text):
        async with self.lock:
            if not self.current_user(uid):
                return False
            if self.active_order is not None:
                return False
            self.lease_until = time.monotonic() + self.session_ttl
            await self.client.send_message(self.peer, text)
            return True

    async def submit_order(self, uid, oid, url):
        async with self.lock:
            if not self.may_start(uid):
                return False
            if self.active_order is not None:
                return False
            if self.owner != uid:
                # Reset source session before starting with a different customer.
                if not await self._claim(uid, reset=True):
                    return False
            else:
                self.lease_until = time.monotonic() + self.session_ttl
            self.active_order = oid
            self.order_owner = uid
            self.lease_until = time.monotonic() + self.result_timeout + 90
            if self.timeout_task:
                self.timeout_task.cancel()
            try:
                await self.client.send_message(self.peer, url)
            except Exception:
                self.active_order = None
                self.order_owner = None
                raise
            self.timeout_task = asyncio.create_task(self._order_timeout(oid,uid))
            return True

    async def _order_timeout(self, oid, uid):
        try:
            await asyncio.sleep(self.result_timeout)
            if self.active_order != oid:
                return
            status = self.finish(oid, "review", "Upstream result timeout")
            self.active_order = None
            self.order_owner = None
            self.lease_until = time.monotonic() + 20
            self.needs_rotation = True
            if self.on_terminal:
                asyncio.create_task(self.on_terminal(self,uid))
            if status != "cancelled":
                await self.app.bot.send_message(
                    chat_id=uid,
                    text="⚠️ No conclusive verification result was received. "
                         "No DigiVerify wallet credits were deducted. Contact support before retrying."
                )
        except asyncio.CancelledError:
            return

    def cancel(self, oid):
        if self.active_order == oid:
            self.active_order = None
            self.order_owner = None
            if self.timeout_task:
                self.timeout_task.cancel()
            # A provider request already submitted cannot be cancelled remotely.
            # Prevent a late response being handed to another customer.
            self.mute_until = time.monotonic() + self.result_timeout
            self.lease_until = self.mute_until

    async def press(self, uid, token):
        async with self.lock:
            entry = self.callbacks.get(token)
            if not entry or entry[0] != uid or not self.current_user(uid):
                return False, "This button is no longer active. Open /start again."
            if self.active_order is not None:
                return False, "A verification request is processing. Please wait for the result."
            _, upid, row, col = entry
            msg = await self.client.get_messages(self.peer, ids=upid)
            if not msg or not msg.buttons:
                return False, "The original button expired. Open /start again."
            try:
                button = msg.buttons[row][col]
            except (IndexError, TypeError):
                return False, "The original button is unavailable. Open /start again."
            original = getattr(button, "button", None)
            if not isinstance(original, types.KeyboardButtonCallback):
                return False, "This button cannot be pressed through Telegram."
            self.lease_until = time.monotonic() + self.session_ttl
            answer = await msg.click(row, col)
            popup = getattr(answer,"message",None) or ""
            return True, popup

    def _markup(self, msg, uid):
        if not msg.reply_markup:
            return None
        if isinstance(msg.reply_markup, types.ReplyKeyboardHide):
            return ReplyKeyboardRemove()
        if not msg.buttons:
            return None
        rows = []
        if isinstance(msg.reply_markup, types.ReplyInlineMarkup):
            for i, line in enumerate(msg.buttons):
                copied = []
                for j, btn in enumerate(line):
                    label = getattr(btn,"text","") or "Open"
                    original = getattr(btn,"button",None)
                    if isinstance(original, (types.KeyboardButtonUrl, types.KeyboardButtonWebView)):
                        address = getattr(btn,"url",None) or getattr(original,"url",None)
                        if address and (address.startswith("https://") or address.startswith("http://") or address.startswith("tg://")):
                            copied.append(InlineKeyboardButton(label, url=address))
                    elif isinstance(original, types.KeyboardButtonCallback):
                        token = secrets.token_urlsafe(9)
                        self.callbacks[token] = (uid,msg.id,i,j)
                        copied.append(InlineKeyboardButton(label, callback_data="up:"+token))
                    # Sharing the account's phone/location or granting web-app permissions
                    # cannot be safely mirrored as a silent automatic action.
                if copied:
                    rows.append(copied)
            return InlineKeyboardMarkup(rows) if rows else None
        if isinstance(msg.reply_markup, types.ReplyKeyboardMarkup):
            labels = [[str(btn.text) for btn in line] for line in msg.buttons if line]
            if labels:
                return ReplyKeyboardMarkup(labels, resize_keyboard=True, one_time_keyboard=False)
        return None

    async def _deliver(self, msg, edited=False):
        """Only customer-specific verification updates leave the source account.

        Source help, top-up, referral, group, wallet and payment information
        stay private in the administrator's upstream Telegram session.
        """
        if not msg or getattr(msg, "out", False):
            return
        if self.observed_callback is not None:
            try:
                await self.observed_callback(self, msg)
            except Exception:
                LOG.exception("Cannot update upstream balance telemetry")
        uid=self.owner
        if uid is None or self.order_owner!=uid or self.active_order is None:
            return
        if time.monotonic()>=self.lease_until or time.monotonic()<self.mute_until:
            return
        text=msg.raw_text or ""
        outcome, detail=self.classify(text)
        if outcome is None:
            lower=text.casefold()
            if any(x in lower for x in ("verification request received","request queued","vip top priority","verification request queued","verification is being processed")):
                if (uid,msg.id) in self.sent:
                    return
                status_msg=(
                    "⏳ DIGIVERIFY — REQUEST QUEUED\n"
                    "Your verification is waiting in the processing queue."
                    if "queue" in lower or "top priority" in lower else
                    "📨 DIGIVERIFY — VERIFICATION REQUEST RECEIVED\n"
                    "Your request is processing. The result will be sent here."
                )
                try:
                    m=await self.app.bot.send_message(chat_id=uid,text=status_msg)
                    self.sent[(uid,msg.id)]=m.message_id
                except Exception:
                    LOG.exception("Unable to deliver verification status")
            # Never forward unrelated source help/credit/payment/group/profile messages.
            return

        oid=self.active_order
        status=self.finish(oid,outcome,detail or "")
        self.active_order=None
        self.order_owner=None
        self.lease_until=time.monotonic()+20
        self.needs_rotation=True
        if self.timeout_task:
            self.timeout_task.cancel()
        if self.on_terminal:
            asyncio.create_task(self.on_terminal(self,uid))
        if status=="success":
            duration=re.search(r"Processing\s*Time\s*:\s*(\d{1,5})\s*s",text,re.IGNORECASE)
            line=(f"\n⏱ Processing Time: {duration.group(1)}s" if duration else "")
            cost="ADMIN TEST — FREE" if self.is_admin(uid) else f"USD {self.price_label.lstrip('$')}"
            await self.app.bot.send_message(
                chat_id=uid,
                text=("🎉 DIGIVERIFY — VERIFICATION SUCCESSFUL! 🎉\n"
                      "━━━━━━━━━━━━━━━━━━"+line+"\n"
                      "🔗 Google One AI (5TB) Activation Link:\n"
                      +str(detail)+"\n\n"
                      "✨ Congratulations! Use the link above to activate your student discount.\n"
                      "💎 DigiVerify Credit Used: "+cost)
            )
        elif status=="failed":
            await self.app.bot.send_message(
                chat_id=uid,
                text=("❌ DIGIVERIFY — VERIFICATION COULD NOT BE COMPLETED\n"
                      "━━━━━━━━━━━━━━━━━━\n"
                      "🛡 ZERO DIGIVERIFY CREDITS DEDUCTED.\n"
                      "🔄 Please refresh your SheerID link and try again.\n"
                      "💰 Your DigiVerify wallet remains unchanged.")
            )

    async def _new_message(self, event):
        await self._deliver(event.message, edited=False)

    async def _edited_message(self, event):
        await self._deliver(event.message, edited=True)
