import asyncio
import unittest
from types import SimpleNamespace

from telethon import types
from telegram import InlineKeyboardMarkup
from mirror import ProviderMirror


class FakeBot:
    def __init__(self):
        self.sent=[]
        self.edits=[]
    async def send_message(self,chat_id=None,text="",**kwargs):
        self.sent.append((chat_id,text,kwargs))
        return SimpleNamespace(message_id=100+len(self.sent))
    async def edit_message_text(self,**kwargs):
        self.edits.append(kwargs)
        return SimpleNamespace(message_id=kwargs["message_id"])


class FakeClient:
    def __init__(self):
        self.sent=[]
        self.events=[]
        self.messages={}
    async def get_entity(self,name):
        return SimpleNamespace(id=901,username=name)
    def add_event_handler(self,func,event):
        self.events.append(func)
    def remove_event_handler(self,func):
        self.events.remove(func)
    async def send_message(self,peer,text):
        self.sent.append(text)
    async def get_messages(self,peer,ids):
        return self.messages.get(ids)


class FakeMessage:
    def __init__(self,id=77,text="Welcome",keyboard=None,buttons=None):
        self.id=id
        self.raw_text=text
        self.reply_markup=keyboard
        self.buttons=buttons
        self.photo=None
        self.document=None
        self.media=None
        self.out=False
        self.clicks=[]
    async def click(self,row,col):
        self.clicks.append((row,col))
        return SimpleNamespace(message="Option selected")


class MirrorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=FakeClient()
        self.bot=FakeBot()
        self.finished=[]
        self.app=SimpleNamespace(bot=self.bot)
        def finish(oid,status,detail):
            self.finished.append((oid,status,detail))
            return status
        def classify(text):
            if "VERIFICATION SUCCESSFUL" in text:
                return "success","https://one.google.com/ai-student?sc=test"
            return None,None
        self.mirror=ProviderMirror(self.client,"gemini_ver_bot",self.app,finish,classify,
                                   lambda uid:uid==100, "$2.50",session_ttl=180,result_timeout=60)
        await self.mirror.start()

    async def asyncTearDown(self):
        await self.mirror.stop()

    async def test_welcome_and_callback_buttons_are_mirrored(self):
        self.assertTrue(await self.mirror.open(100))
        self.assertEqual(self.client.sent,["/start"])
        btn=types.KeyboardButtonCallback(text="📚 Edu Services",data=b"catalog")
        markup=types.ReplyInlineMarkup(rows=[[btn]])
        msg=FakeMessage(keyboard=markup,buttons=[[SimpleNamespace(text=btn.text,button=btn)]])
        self.client.messages[77]=msg
        await self.mirror._deliver(msg)
        self.assertEqual(self.bot.sent[0][1],"Welcome")
        inline=self.bot.sent[0][2]["reply_markup"]
        self.assertIsInstance(inline,InlineKeyboardMarkup)
        token=inline.inline_keyboard[0][0].callback_data
        self.assertTrue(token.startswith("up:"))
        ok,popup=await self.mirror.press(100,token[3:])
        self.assertTrue(ok)
        self.assertEqual(popup,"Option selected")
        self.assertEqual(msg.clicks,[(0,0)])

    async def test_one_upstream_account_is_lease_locked(self):
        self.assertTrue(await self.mirror.open(100))
        self.assertFalse(await self.mirror.open(200))
        self.assertFalse(await self.mirror.send_text(200,"Top Up"))
        self.assertTrue(await self.mirror.release(100))
        self.assertTrue(await self.mirror.open(200))
        self.assertEqual(self.client.sent,["/start","/start"])

    async def test_verification_reply_is_forwarded_and_settled(self):
        self.assertTrue(await self.mirror.open(100))
        self.assertTrue(await self.mirror.submit_order(100,33,"https://services.sheerid.com/verify/test"))
        message=FakeMessage(id=88,text="🎉 VERIFICATION SUCCESSFUL!\nhttps://one.google.com/ai-student?sc=test")
        await self.mirror._deliver(message)
        self.assertEqual(self.finished[0][0:2],(33,"success"))
        self.assertTrue(any("VERIFICATION SUCCESSFUL" in x[1] for x in self.bot.sent))
        self.assertTrue(any("ADMIN TEST" in x[1] for x in self.bot.sent))
        self.assertIsNone(self.mirror.active_order)

    async def test_existing_message_edit_updates_same_local_message(self):
        self.assertTrue(await self.mirror.open(200))
        first=FakeMessage(id=91,text="Queue position: 3")
        await self.mirror._deliver(first)
        update=FakeMessage(id=91,text="Queue position: 2")
        await self.mirror._deliver(update,edited=True)
        self.assertEqual(self.bot.edits[0]["message_id"],101)
        self.assertEqual(self.bot.edits[0]["text"],"Queue position: 2")


if __name__=="__main__":
    unittest.main()
