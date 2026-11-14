"""Onboard each authorized Telegram user session with the upstream bot.

Only the session owner account joins the provider's own group. A session
that cannot complete setup is kept out of the customer allocation pool.
"""
import asyncio
import logging
import os
import re
from urllib.parse import urlparse
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

LOG=logging.getLogger(__name__)
GROUP_LINK=re.compile(r"https?://t\.me/(?:joinchat/|\+)?[A-Za-z0-9_-]+",re.I)

def _buttons(messages):
    for msg in messages:
        for row in getattr(getattr(msg,"reply_markup",None),"rows",[]) or []:
            for button in row.buttons:
                yield msg,button

async def _messages(client,peer):
    return await client.get_messages(peer,limit=12)

def _has_ready_message(messages):
    return any(any(s in (getattr(m,"raw_text","") or "").lower() for s in
                   ("edu services","top up","history & balance","vip priority turns","free / standard turns"))
               for m in messages)

async def setup_provider_account(client,provider):
    peer=await client.get_entity(provider.lstrip("@"))
    await client.send_message(peer,"/start")
    await asyncio.sleep(2)
    messages=await _messages(client,peer)
    # Explicit English selection, only if the upstream bot offers it.
    english=None
    for msg,button in _buttons(messages):
        label=str(getattr(button,"text","") or "").lower()
        if "english" in label or "🇬🇧" in label or "🇺🇸" in label:
            english=(msg,button);break
    if english:
        msg,button=english
        try:
            await msg.click(data=getattr(button,"data",None)) if getattr(button,"data",None) else await msg.click(text=button.text)
        except Exception as exc:
            return False,"English selection failed: "+type(exc).__name__
        await asyncio.sleep(2)
        messages=await _messages(client,peer)
    # Find the source group's explicit invitation; never guess a group.
    group_links=[]
    for msg in messages:
        group_links.extend(GROUP_LINK.findall(getattr(msg,"raw_text","") or ""))
        for _,button in _buttons([msg]):
            url=getattr(button,"url",None)
            if url and GROUP_LINK.fullmatch(url):group_links.append(url)
    configured=os.getenv('PROVIDER_GROUP_LINK','https://t.me/+MHk3r7Zh0kFiYTI1').strip()
    if configured and GROUP_LINK.fullmatch(configured):group_links.insert(0,configured)
    # Provider may be ready without a mandatory group; do not force an unknown join.
    if not group_links and _has_ready_message(messages):
        return True,"Provider ready; English menu detected (no group join prompt)"
    if not group_links:
        return False,"Provider group invitation not found; manual setup required"
    for url in dict.fromkeys(group_links):
        path=urlparse(url).path.strip("/")
        try:
            if path.startswith("+"):
                await client(ImportChatInviteRequest(path[1:]))
            elif path.startswith("joinchat/"):
                await client(ImportChatInviteRequest(path.split("/",1)[1]))
            else:
                await client(JoinChannelRequest(path))
        except Exception as exc:
            if "ALREADY_PARTICIPANT" not in str(exc).upper() and "USER_ALREADY_PARTICIPANT" not in str(exc).upper() and type(exc).__name__!="UserAlreadyParticipantError":
                return False,"Provider group join failed: "+type(exc).__name__+" "+str(exc)[:90]
    await client.send_message(peer,"/start")
    await asyncio.sleep(2)
    messages=await _messages(client,peer)
    if _has_ready_message(messages):
        return True,"English and provider group setup completed"
    return False,"Joined provider group; provider access not yet confirmed"
