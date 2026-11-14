import asyncio
import unittest
from types import SimpleNamespace
from mirror import ProviderMirror

class FakeBot:
    def __init__(self):
        self.sent=[]
    async def send_message(self,chat_id=None,text="",**kwargs):
        self.sent.append((chat_id,text,kwargs))
        return SimpleNamespace(message_id=100+len(self.sent))

class FakeClient:
    def __init__(self):
        self.sent=[]
        self.events=[]
    async def get_entity(self,name):
        return SimpleNamespace(id=901,username=name)
    def add_event_handler(self,func,event):
        self.events.append(func)
    def remove_event_handler(self,func):
        self.events.remove(func)
    async def send_message(self,peer,text):
        self.sent.append(text)

class FakeMessage:
    def __init__(self,id=77,text="Welcome"):
        self.id=id
        self.raw_text=text
        self.out=False

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
            lower=text.lower()
            if "verification successful" in lower:
                return "success","https://one.google.com/ai-student?sc=test"
            if "verification could not be completed" in lower:
                return "failed",""
            return None,None
        self.mirror=ProviderMirror(self.client,"gemini_ver_bot",self.app,finish,classify,
                                   lambda uid:uid==100,"$2.50",session_ttl=180,result_timeout=60)
        await self.mirror.start()

    async def asyncTearDown(self):
        await self.mirror.stop()

    async def test_source_welcome_and_buttons_not_leaked(self):
        self.assertTrue(await self.mirror.open(100))
        await self.mirror._deliver(FakeMessage(text="WELCOME TO GEMINI SHEERID VERIFIER!\n💰 Top Up\n@originalsupport"))
        self.assertEqual(len(self.bot.sent),0)

    async def test_other_customer_cannot_use_account(self):
        self.assertTrue(await self.mirror.open(100))
        self.assertFalse(await self.mirror.open(200))
        self.assertFalse(await self.mirror.send_text(200,"Top Up"))
        self.assertTrue(await self.mirror.release(100))
        self.assertTrue(await self.mirror.open(200))

    async def test_verification_result_is_clean_and_customer_priced(self):
        self.assertTrue(await self.mirror.open(100))
        self.assertTrue(await self.mirror.submit_order(100,33,"https://services.sheerid.com/verify/test"))
        await self.mirror._deliver(FakeMessage(id=80,text="💰 TOP UP / BUY CREDITS\nOriginal provider @mynameric999\n0x123"))
        self.assertEqual(self.bot.sent,[])
        await self.mirror._deliver(FakeMessage(id=81,text="💎 VIP REQUEST QUEUED WITH TOP PRIORITY\n$0.60\nOriginal provider balance 3 VIP"))
        self.assertEqual(len(self.bot.sent),1)
        self.assertNotIn("0.60",self.bot.sent[0][1])
        self.assertNotIn("VIP",self.bot.sent[0][1])
        await self.mirror._deliver(FakeMessage(id=82,text="🎉 VERIFICATION SUCCESSFUL!\n⏱ Processing Time: 149s\nhttps://one.google.com/ai-student?sc=test\nCredit Used 1 VIP\n@originalsupport"))
        self.assertEqual(self.finished[0][0:2],(33,"success"))
        texts="\n".join(x[1] for x in self.bot.sent)
        self.assertIn("VERIFICATION SUCCESSFUL",texts)
        self.assertIn("https://one.google.com/ai-student?sc=test",texts)
        self.assertNotIn("@originalsupport",texts)
        self.assertNotIn("Credit Used 1 VIP",texts)
        self.assertIn("ADMIN TEST",texts)
        self.assertIsNone(self.mirror.active_order)

    async def test_source_messages_after_completion_suppressed(self):
        await self.mirror.open(100)
        await self.mirror.submit_order(100,55,"https://services.sheerid.com/verify/example")
        await self.mirror._deliver(FakeMessage(id=88,text="❌ VERIFICATION COULD NOT BE COMPLETED\nOriginal support group: https://t.me/original"))
        self.assertIsNone(self.mirror.active_order)
        count=len(self.bot.sent)
        await self.mirror._deliver(FakeMessage(id=89,text="Join our original payment group at https://t.me/original"))
        self.assertEqual(len(self.bot.sent),count)
        self.assertNotIn("original",self.bot.sent[0][1].lower())

if __name__=="__main__":unittest.main()
