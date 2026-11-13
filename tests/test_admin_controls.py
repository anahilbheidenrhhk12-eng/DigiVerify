import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
import bot

class AdminControlsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        bot.PATH=os.path.join(self.tmp.name,"admin.sqlite3")
        bot.ADMINS={1001}
        bot.Q=asyncio.Queue()
        bot.db().close()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def msg(self):
        class Message:
            def __init__(self): self.responses=[]
            async def reply_text(self,text,**kw): self.responses.append(text)
        return Message()

    async def test_add_admin_persists_and_has_free_access(self):
        message=self.msg()
        u=SimpleNamespace(effective_user=SimpleNamespace(id=1001),message=message)
        await bot.addadmin(u,SimpleNamespace(args=["2002"]))
        self.assertTrue(bot.is_admin(2002))
        self.assertIn("ADMIN TEST MODE",bot.verify_text(2002))
        self.assertEqual(bot.balance(2002)[0],0)

    async def test_nonadmin_cannot_add_admin(self):
        u=SimpleNamespace(effective_user=SimpleNamespace(id=3003),message=self.msg())
        await bot.addadmin(u,SimpleNamespace(args=["4004"]))
        self.assertFalse(bot.is_admin(4004))

    async def test_cancel_queued_order_without_charge(self):
        uid=3003
        bot.user(uid)
        with bot.closing(bot.db()) as con:
            con.execute("UPDATE users SET balance=500 WHERE uid=?",(uid,))
            con.commit()
        with bot.closing(bot.db()) as con:
            oid=con.execute("INSERT INTO orders(uid,url,status,created) VALUES (?,?,?,?)",(uid,"https://services.sheerid.com/verify/example","queued",0)).lastrowid
            con.commit()
        msg=self.msg()
        notifications=[]
        async def send_message(uid,text): notifications.append((uid,text))
        u=SimpleNamespace(effective_user=SimpleNamespace(id=1001),effective_message=msg)
        c=SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
        await bot.cancel_order(u,c,oid)
        self.assertEqual(bot.finish(oid,"success"),"cancelled")
        self.assertEqual(bot.balance(uid)[0],500)
        self.assertEqual(len(notifications),1)
        self.assertIn("cancelled",msg.responses[0].lower())

if __name__=="__main__": unittest.main()
