import os,asyncio,tempfile,unittest
from types import SimpleNamespace
import bot

class FakeMessage:
    def __init__(self,text=""):
        self.text=text
        self.sent=[]
    async def reply_text(self,text,**kwargs):
        self.sent.append(text)

class FakeBot:
    def __init__(self):
        self.notifications=[]
    async def send_message(self,chat_id=None,text="",**kwargs):
        self.notifications.append((chat_id,text))
    async def get_me(self):
        return SimpleNamespace(username="DigitopVerifyBot")

class ShopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir=tempfile.TemporaryDirectory()
        self.oldpath=bot.PATH
        self.oldadmins=bot.ADMINS
        self.oldmirror=bot.MIRROR
        bot.PATH=os.path.join(self.dir.name,"shop.sqlite3")
        bot.ADMINS={101}
        bot.MIRROR=None
        bot.db().close()
        bot.SHOP.setup()
        self.backend=FakeBot()

    async def asyncTearDown(self):
        bot.PATH=self.oldpath
        bot.ADMINS=self.oldadmins
        bot.MIRROR=self.oldmirror
        self.dir.cleanup()

    def update(self,uid=202,name="Paying Customer",text=""):
        m=FakeMessage(text)
        return SimpleNamespace(effective_user=SimpleNamespace(id=uid,first_name=name),
                               effective_message=m,message=m)

    def ctx(self,args=None):
        return SimpleNamespace(bot=self.backend,args=args or [])

    async def test_start_and_history_display_customer_not_source(self):
        u=self.update()
        await bot.start(u,self.ctx())
        self.assertIn("Paying Customer",u.effective_message.sent[0])
        self.assertIn("DigiVerify",u.effective_message.sent[0])
        self.assertNotIn("Eric",u.effective_message.sent[0])
        self.assertFalse(any("GitHub Reward" in x.text for row in bot.menu().inline_keyboard for x in row))
        await bot.history(u,self.ctx())
        self.assertIn("Customer: Paying Customer",u.effective_message.sent[-1])

    async def test_top_up_is_own_wallet_and_requests_support_when_unconfigured(self):
        u=self.update()
        await bot.SHOP.topup(u,self.ctx())
        msg=u.effective_message.sent[-1]
        self.assertIn("DIGIVERIFY",msg)
        self.assertIn("not configured",msg)
        self.assertNotIn("gemini_ver_bot",msg)
        self.assertNotIn("0x1e3e1cd126071d509a41a4f71ad2b4f142477a79",msg)

    async def test_customer_tx_pending_until_admin_confirms_once(self):
        address="0x"+"a"*40
        u=self.update(uid=101,name="Admin")
        await bot.SHOP.configure(u,self.ctx([address]),"payment")
        customer=self.update()
        bot.user(202)
        await bot.SHOP.choose_amount(customer,self.ctx(),500)
        self.assertIn(address,customer.effective_message.sent[-1])
        tx="0x"+"f"*64
        self.assertTrue(await bot.SHOP.submit_tx(customer,self.ctx(),tx))
        self.assertEqual(bot.balance(202)[0],0)
        with bot.closing(bot.db()) as con:
            order=con.execute("SELECT id FROM customer_topups WHERE uid=202").fetchone()
        await bot.SHOP.review(u,self.ctx([str(order["id"])]),True)
        self.assertEqual(bot.balance(202)[0],500)
        await bot.SHOP.review(u,self.ctx([str(order["id"])]),True)
        self.assertEqual(bot.balance(202)[0],500)

    async def test_support_group_does_not_inherit_source_links(self):
        u=self.update(uid=101,name="Admin")
        await bot.SHOP.configure(u,self.ctx(["https://t.me/our_digiverify_group"]),"group")
        customer=self.update()
        await bot.SHOP.support(customer,self.ctx())
        self.assertIn("our_digiverify_group",customer.effective_message.sent[-1])
        self.assertNotIn("mynameric",customer.effective_message.sent[-1])

if __name__=="__main__":unittest.main()
